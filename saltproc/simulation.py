import numpy as np
import tables as tb
import os
from collections import OrderedDict


class Simulation():
    """Class for handling simulation information. Contains information
    for running simulation wiht parallelism. Also contains the simulation
    name, a `Depcode` object, and the filename for the simulation database.
    Contains methods to store simulation metadata and depletion results in
    a database, predict reactor criticality at next depletion step, and
    switch simulation geometry.

    Parameters
    ----------
    sim_name : str
        Name to identify the simulation. May contain information such as
        the number of a reference case, a paper name, or some other
        specific information identify the simulation.
    sim_depcode : `Depcode` object
        An instance of one of the `Depcode` child classes
    db_path : str
        Path of HDF5 database that stores simulation information and
        data.
    restart_flag : bool
        This value determines our initial condition. If `True`, then
        then we run the simulation starting from the inital material
        composition in the material input file inside our `depcode`
        object. If `False`, then we runthe simulation starting from
        the final material composition resulting within the `.h5`
        database.
    adjust_geo : bool
        This value determines if we switch reactor geometry when keff
        drops below 1.0
    compression_params : Pytables filter object
        Compression parameters for HDF5 database.

    """

    def __init__(
            self,
            sim_name="default",
            sim_depcode="depcode",
            db_path="db_saltproc.h5",
            restart_flag=True,
            adjust_geo=False,
            compression_params=tb.Filters(complevel=9,
                                          complib='blosc',
                                          fletcher32=True),
    ):
        """Initializes the Simulation object.

        """
        # initialize all object attributes
        self.sim_name = sim_name
        self.sim_depcode = sim_depcode
        self.db_path = db_path
        self.restart_flag = restart_flag
        self.adjust_geo = adjust_geo
        self.compression_params = compression_params
        self.nuclide_indices_dtype = np.dtype([('nuclide', 'S9'),
                                          ('index', int)])

    def check_restart(self):
        """If the user set `restart_flag`
        for `False` clean out iteration files and database from previous run.

        Parameters
        ----------
        restart_flag : bool
            Is the current simulation restarted?

        Returns
        -------
        failed_step : int
            The depletion step that the simulation failed on.

        """
        if not self.restart_flag:
            failed_step = 0
            try:
                os.remove(self.db_path)
                os.remove(self.sim_depcode.runtime_matfile)
                if isinstance(self.sim_depcode.runtime_inputfile, dict):
                    for  value in self.sim_depcode.runtime_inputfile.values():
                        os.remove(value)
                else:
                    os.remove(self.sim_depcode.runtime_inputfile)
                print("Previous run output files were deleted.")
            except OSError as e:
                pass
        else:
            db = tb.open_file(
                self.db_path,
                mode='r')
            failed_step = len(db.root.simulation_parameters.col('keff_eds'))
            db.close()
        return failed_step



    def store_after_repr(self, after_mats, waste_dict, dep_step):
        """Add data for waste streams [grams per depletion step] of each
        process to the HDF5 database after reprocessing.

        Parameters
        ----------
        after_mats : `Materialflow`
            `Materialflow` object representing a material stream after
            performing reprocessing.
        waste_dict : dict of str to Materialflow
            Dictionary that maps `Process` objects to waste `Materialflow`
            objects.

            ``key``
                `Process` name (`str`)
            ``value``
                `Materialflow` object containing waste streams data.
        dep_step : int
            Current depletion time step.

        """
        if waste_dict is not None:
            streams_description = 'in_out_streams'
            db = tb.open_file(
                self.db_path,
                mode='a',
                filters=self.compression_params)
            for material_name in waste_dict.keys():  # iterate over materials
                mat_node = getattr(db.root.materials, material_name)
                if not hasattr(mat_node, streams_description):
                    waste_group = db.create_group(
                        mat_node,
                        streams_description,
                        'Waste stream compositions for each process')
                else:
                    waste_group = getattr(mat_node, streams_description)
                for proc in waste_dict[material_name].keys():
                    if not hasattr(waste_group, proc):
                        proc_node = db.create_group(waste_group, proc)
                    else:
                        proc_node = getattr(waste_group, proc)
                    nuclide_indices = []
                    iso_wt_frac = []
                    coun = 0
                    if hasattr(waste_dict[material_name][proc], 'comp'):
                        # Read isotopes from Materialflow
                        for nuc, wt_frac in waste_dict[material_name][proc].comp.items():
                            # Dictonary in format {isotope_name : index(int)}
                            nuclide_indices.append((nuc, coun))
                            # Convert wt% to absolute [user units]
                            iso_wt_frac.append(wt_frac * waste_dict[material_name][proc].mass)
                            coun += 1
                        # Try to open EArray and table and if not exist - create
                        nuclide_indices_array = np.array(nuclide_indices, dtype=self.nuclide_indices_dtype)
                        if hasattr(proc_node, 'comp'):
                            earr = db.get_node(proc_node, 'comp')
                        else:
                            earr = db.create_earray(
                                proc_node,
                                'comp',
                                atom=tb.Float64Atom(),
                                shape=(0, len(nuclide_indices_array)),
                                title="Isotopic composition for %s" % proc)
                            # Save isotope indexes map and units in EArray attributes
                            earr.flavor = 'python'
                        if not hasattr(proc_node, 'nuclide_map'):
                            db.create_table(proc_node,
                                           'nuclide_map',
                                            description=nuclide_indices_array)

                        earr, iso_wt_frac = self._fix_nuclide_discrepancy(db, earr, nuclide_indices, iso_wt_frac)

                        earr.append(np.asarray([iso_wt_frac], dtype=np.float64))
                        del iso_wt_frac, nuclide_indices
            db.close()
        # Also save materials AFTER reprocessing and refill here
        self.store_mat_data(after_mats, dep_step, True)

    def _fix_nuclide_discrepancy(self, db, earr, nuclide_indices, iso_wt_frac):
        """Fix discrepancies between nuclide keys present in stored results and
        nuclides keys stored in results for the current depletion step

        Parameters
        ----------
        db : tables.File
            The SaltProc results database
        earr : tables.EArray
            Array storing nuclide material mass compositions from previously
            completed depletion steps
        nuclide_indices : OrderedDict
            Map of nuclide name to array index
        iso_wt_frac : list of float
            List storing nuclide material mass compositions for current
            depletion step.

        Returns
        -------
        earr : tables.EArray
            Array storing nuclide material mass compositions from
            previously completed depletion steps with additional
            rows for nuclides introduces in iso_wt_frac
        iso_wt_frac : list of float
            List storing nuclide material mass compositions for current
            depletion step with additional entries for nuclides not present
            in the current depletion step that are stored in earr.
        """

        parent_node = earr._v_parent
        base_nucs = set(map(bytes.decode, parent_node.nuclide_map.col('nuclide')))
        iso_idx = dict(nuclide_indices)
        step_nucs = set(iso_idx.keys())
        forward_difference = base_nucs.difference(step_nucs)
        backward_difference = step_nucs.difference(base_nucs)

        if len(backward_difference) > 0 or len(forward_difference) > 0:
            combined_nucs, combined_map, combined_earr, combined_step_arr = \
                self._add_missing_nuclides(base_nucs, step_nucs, earr, iso_idx, iso_wt_frac)

            node_name = earr.name
            node_title = earr.title
            parent_node = earr._v_parent
            # We have to rewrite all the data because EArrays are only extensible in one dimension
            db.remove_node(earr)
            earr = db.create_earray(
                        parent_node,
                        node_name,
                        atom=tb.Float64Atom(),
                        shape=(0, len(combined_nucs)),
                        title=node_title)
            # Save isotope indexes map and units in EArray attributes
            earr.flavor = 'python'
            #earr.attrs.iso_map = combined_map
            earr_len = len(combined_earr)
            for i in range(earr_len):
                earr.append(np.array([combined_earr[i]]))

            # Reform nuclide_map
            nuclide_indices = list(zip(combined_map.keys(), combined_map.values()))
            nuclide_indices_array = np.array(nuclide_indices, dtype=self.nuclide_indices_dtype)
            db.remove_node(parent_node.nuclide_map)
            db.create_table(parent_node,
                            'nuclide_map',
                            description=nuclide_indices_array)
        else:
            combined_step_arr = iso_wt_frac

        return earr, combined_step_arr

    def _add_missing_nuclides(self, base_nucs, step_nucs, earr, iso_idx, iso_wt_frac):
        """Add missing nuclides to stored results and the results for the
        current depletion step

        Parameters
        ----------
        base_nucs : set
            Nuclides present in previous depletion steps
        step_nucs : set
            Nuclides present in current depletion step
        earr : tables.EArray
            Array storing nuclide material mass compositions from previously
            completed depletion steps
        iso_idx : OrderedDict
            Map of nuclide name to array index
        iso_wt_frac : list of float
            List storing nuclide material mass compositions for current
            depletion step.

        Returns
        -------
        combined_nucs : numpy.ndarray
            Nuclide-code sorted union of base_nucs and step_nucs
        combined_map : OrderedDict
            Map of nuclide names to array index
        combined_earr : numpy.ndarray
            Array storing nuclide material mass compositions from
            previously completed depletion steps with additional
            rows for nuclides introduces in iso_wt_frac
        combined_step_arr : numpy.ndarray
            Array storing nuclide material mass compositions for current
            depletion step with additional entries for nuclides not present
            in the current depletion step that are stored in earr.
        """
        parent_node = earr._v_parent
        _nuclides = list(map(bytes.decode, parent_node.nuclide_map.col('nuclide')))
        _indices = parent_node.nuclide_map.col('index')
        base_nuclide_map = dict(zip(_nuclides, _indices))
        combined_nucs = list(base_nucs.union(step_nucs))
        # Sort the nucnames by ZAM
        nuclide_codes = list(map(self.sim_depcode.name_to_nuclide_code, combined_nucs))
        combined_nucs = [nucname for nuclide_code, nucname in sorted(zip(nuclide_codes,combined_nucs))]

        combined_values = np.arange(0, len(combined_nucs), 1).tolist()
        combined_map = OrderedDict(zip(combined_nucs,combined_values))
        combined_earr = np.zeros((len(earr), len(combined_map)))
        combined_step_arr = np.zeros(len(combined_map))
        earr_len = len(earr)
        # not efficient, but can't come up with a better way right now
        for nuc, idx in combined_map.items():
            if nuc in base_nucs:
                for i in range(earr_len):
                    combined_earr[i,idx] = earr[i][base_nuclide_map[nuc]]
                if nuc in step_nucs:
                    combined_step_arr[idx] = iso_wt_frac[iso_idx[nuc]]
                else:
                    combined_step_arr[idx] = 0.0

            elif nuc in step_nucs:
                for i in range(earr_len):
                    combined_earr[i,idx] = 0.0
                combined_step_arr[idx] = iso_wt_frac[iso_idx[nuc]]
        return combined_nucs, combined_map, combined_earr, combined_step_arr

    def store_mat_data(self, mats, dep_step, store_at_end=False):
        """Initialize the HDF5/Pytables database (if it doesn't exist) or
        append the following data at the current depletion step to the
        database: burnable material composition, mass, density, volume,
        burnup,  mass_flowrate, void_fraction.

        Parameters
        ----------
        mats : dict of str to Materialflow
            Dictionary that contains `Materialflow` objects.

            ``key``
                Name of burnable material.
            ``value``
                `Materialflow` object holding composition and properties.
        dep_step : int
            Current depletion step.
        store_at_end : bool, optional
            Controls at which moment in the depletion step to store data from.
            If `True`, the function stores data from the end of the
            depletion step. Otherwise, the function stores data from the
            beginning of the depletion step.

        """
        # Determine moment in depletion step from which to store data
        if store_at_end:
            dep_step_str = ["after_reproc", "after"]
        else:
            dep_step_str = ["before_reproc", "before"]

        # Moment when store compositions
        # numpy array row storage data for material physical properties
        mpar_dtype = np.dtype([
            ('mass', float),
            ('density', float),
            ('volume', float),
            ('mass_flowrate', float),
            ('void_fraction', float),
            ('burnup', float)
        ])

        print(
            '\nStoring material data for depletion step #%i.' %
            (dep_step + 1))
        db = tb.open_file(
            self.db_path,
            mode='a',
            filters=self.compression_params)
        if not hasattr(db.root, 'materials'):
            comp_group = db.create_group('/',
                                         'materials',
                                         'Material data')
        # Iterate over all materials
        for key, value in mats.items():
            nuclide_indices = []
            iso_wt_frac = []
            coun = 0
            # Create group for each material
            if not hasattr(db.root.materials, key):
                db.create_group(comp_group,
                                key)
            # Create group for composition and parameters before reprocessing
            mat_node = getattr(db.root.materials, key)
            if not hasattr(mat_node, dep_step_str[0]):
                db.create_group(mat_node,
                                dep_step_str[0],
                                'Material data {dep_step_str[1]} reprocessing')
            comp_pfx = '/materials/' + str(key) + '/' + dep_step_str[0]
            # Order the nucnames by ZAM
            nuclide_codes = list(map(self.sim_depcode.name_to_nuclide_code, mats[key].comp.keys()))
            ordered_nucs = [nucname for nuclide_code, nucname in sorted(zip(nuclide_codes,mats[key].comp.keys()))]
            # Read isotopes from Materialflow for material
            for nuc in ordered_nucs:
                wt_frac = mats[key].comp[nuc]
                # Dictonary in format {isotope_name : index(int)}
                nuclide_indices.append((nuc, coun))
                # Convert wt% to total mass [g]
                iso_wt_frac.append(wt_frac * mats[key].mass)
                coun += 1
            nuclide_indices_array = np.array(nuclide_indices,
                                             dtype=self.nuclide_indices_dtype)
            # Store information about material properties in new array row
            mpar_row = (
                mats[key].mass,
                mats[key].get_density(),
                mats[key].volume,
                mats[key].mass_flowrate,
                mats[key].void_frac,
                mats[key].burnup
            )
            mpar_array = np.array([mpar_row], dtype=mpar_dtype)
            # Try to open EArray and table and if not exist - create new one
            try:
                earr = db.get_node(comp_pfx, 'comp')
                print(str(earr.title) + ' array exist, appending data.')
                mpar_table = db.get_node(comp_pfx, 'parameters')
            except Exception:
                print(
                    'Material ' +
                    key +
                    ' array is not exist, making new one.')
                earr = db.create_earray(
                    comp_pfx,
                    'comp',
                    atom=tb.Float64Atom(),
                    shape=(0, len(nuclide_indices_array)),
                    title="Isotopic composition for %s" % key)
                # Save isotope indexes map and units in EArray attributes
                earr.flavor = 'python'
                # Create table for material Parameters
                print('Creating ' + key + ' lookup table.')
                db.create_table(comp_pfx,
                                'nuclide_map',
                                description=nuclide_indices_array)
                print('Creating ' + key + ' parameters table.')
                mpar_table = db.create_table(
                    comp_pfx,
                    'parameters',
                    np.empty(0, dtype=mpar_dtype),
                    title="Material parameters data")
            print('Dumping Material %s data %s to %s.' %
                  (key, dep_step_str[0], os.path.abspath(self.db_path)))

            earr, iso_wt_frac = self._fix_nuclide_discrepancy(db, earr, nuclide_indices, iso_wt_frac)

            # Add row for the timestep to EArray and Material Parameters table
            earr.append(np.array([iso_wt_frac], dtype=np.float64))
            mpar_table.append(mpar_array)
            del (iso_wt_frac)
            del (mpar_array)
            mpar_table.flush()
        db.close()

    def store_step_neutronics_parameters(self):
        """Adds the following depletion code and SaltProc simulation
        data at the current depletion step to the database:
        execution time, memory usage, multiplication factor, breeding ratio,
        delayed neutron precursor data, fission mass, cumulative depletion
        time, power level.
        """

        # Read info from depcode _res.m File
        self.sim_depcode.read_neutronics_parameters()
        # Initialize beta groups number
        b_g = len(self.sim_depcode.neutronics_parameters['beta_eff_bds'])
        # numpy array row storage for run info

        class Step_info(tb.IsDescription):
            keff_bds = tb.Float32Col((2,))
            keff_eds = tb.Float32Col((2,))
            breeding_ratio_bds = tb.Float32Col((2,))
            breeding_ratio_eds = tb.Float32Col((2,))
            cumulative_time_at_eds = tb.Float32Col()
            power_level = tb.Float32Col()
            beta_eff_bds = tb.Float32Col((b_g, 2))
            beta_eff_eds = tb.Float32Col((b_g, 2))
            delayed_neutrons_lambda_bds = tb.Float32Col((b_g, 2))
            delayed_neutrons_lambda_eds = tb.Float32Col((b_g, 2))
            fission_mass_bds = tb.Float32Col()
            fission_mass_eds = tb.Float32Col()
        # Open or restore db and append data to it
        db = tb.open_file(
            self.db_path,
            mode='a',
            filters=self.compression_params)
        try:
            step_info_table = db.get_node(
                db.root,
                'simulation_parameters')
            # Read burn_time from previous step
            self.burn_time = step_info_table.col('cumulative_time_at_eds')[-1]
        except Exception:
            step_info_table = db.create_table(
                db.root,
                'simulation_parameters',
                Step_info,  # self.sim_depcode.Step_info,
                "Simulation parameters after each timestep")
            # Intializing burn_time array at the first depletion step
            self.burn_time = 0.0
        self.burn_time += self.sim_depcode.neutronics_parameters['burn_days']
        # Define row of table as step_info
        step_info = step_info_table.row
        # Define all values in the row

        step_info['keff_bds'] = self.sim_depcode.neutronics_parameters['keff_bds']
        step_info['keff_eds'] = self.sim_depcode.neutronics_parameters['keff_eds']
        step_info['breeding_ratio_bds'] = self.sim_depcode.neutronics_parameters[
            'breeding_ratio_bds']
        step_info['breeding_ratio_eds'] = self.sim_depcode.neutronics_parameters[
            'breeding_ratio_eds']
        step_info['cumulative_time_at_eds'] = self.burn_time
        step_info['power_level'] = self.sim_depcode.neutronics_parameters['power_level']
        step_info['beta_eff_bds'] = self.sim_depcode.neutronics_parameters[
            'beta_eff_bds']
        step_info['beta_eff_eds'] = self.sim_depcode.neutronics_parameters[
            'beta_eff_eds']
        step_info['delayed_neutrons_lambda_bds'] = self.sim_depcode.neutronics_parameters[
            'delayed_neutrons_lambda_bds']
        step_info['delayed_neutrons_lambda_eds'] = self.sim_depcode.neutronics_parameters[
            'delayed_neutrons_lambda_eds']
        step_info['fission_mass_bds'] = self.sim_depcode.neutronics_parameters[
            'fission_mass_bds']
        step_info['fission_mass_eds'] = self.sim_depcode.neutronics_parameters[
            'fission_mass_eds']

        # Inject the Record value into the table
        step_info.append()
        step_info_table.flush()
        db.close()

    def store_depcode_metadata(self):
        """Adds the following depletion code and SaltProc simulation parameters
        to the database:
        neutron population, active cycles, inactive cycles, depletion code
        version simulation title, depetion code input file path, depletion code
        working directory, cross section data path, # of OMP threads, # of MPI
        tasks, memory optimization mode (Serpent), depletion timestep size.

        """
        # numpy arraw row storage for run info
        # delete and make this datatype specific
        # to Depcode subclasses
        depcode_metadata_dtype = np.dtype([
            ('depcode_name', 'S20'),
            ('depcode_version', 'S20'),
            ('title', 'S90'),
            ('depcode_input_filename', 'S90'),
            ('depcode_working_dir', 'S90'),
            ('xs_data_path', 'S90')
        ])
        # Read info from depcode _res.m File
        self.sim_depcode.read_depcode_metadata()
        # Store information about material properties in new array row
        depcode_metadata_row = (
            self.sim_depcode.depcode_metadata['depcode_name'],
            self.sim_depcode.depcode_metadata['depcode_version'],
            self.sim_depcode.depcode_metadata['title'],
            self.sim_depcode.depcode_metadata['depcode_input_filename'],
            self.sim_depcode.depcode_metadata['depcode_working_dir'],
            self.sim_depcode.depcode_metadata['xs_data_path']
        )
        depcode_metadata_array = np.array([depcode_metadata_row], dtype=depcode_metadata_dtype)

        # Open or restore db and append datat to it
        db = tb.open_file(
            self.db_path,
            mode='a',
            filters=self.compression_params)
        try:
            depcode_metadata_table = db.get_node(db.root, 'depcode_metadata')
        except Exception:
            depcode_metadata_table = db.create_table(
                db.root,
                'depcode_metadata',
                depcode_metadata_array,
                "Depletion code metadata")
        depcode_metadata_table.flush()
        db.close()

    def store_step_metadata(self):
        """Adds the following depletion code and SaltProc simulation parameters
        to the database:
        neutron population, active cycles, inactive cycles, # of OMP threads, # of MPI
        tasks, memory optimization mode (Serpent), depletion timestep size.

        """
        # numpy arraw row storage for run info
        # delete and make this datatype specific
        # to Depcode subclasses
        step_metadata_dtype = np.dtype([
            ('neutron_population', int),
            ('active_cycles', int),
            ('inactive_cycles', int),
            ('OMP_threads', int),
            ('MPI_tasks', int),
            ('memory_optimization_mode', int),
            ('depletion_timestep_size', float),
            ('execution_time', float),
            ('memory_usage', float)
        ])
        # Read info from depcode _res.m File
        self.sim_depcode.read_step_metadata()
        # Store information about material properties in new array row
        step_metadata_row = (
            self.sim_depcode.npop,
            self.sim_depcode.active_cycles,
            self.sim_depcode.inactive_cycles,  # delete the below
            self.sim_depcode.step_metadata['OMP_threads'],
            self.sim_depcode.step_metadata['MPI_tasks'],
            self.sim_depcode.step_metadata['memory_optimization_mode'],
            self.sim_depcode.step_metadata['depletion_timestep_size'],
            self.sim_depcode.step_metadata['step_execution_time'],
            self.sim_depcode.step_metadata['step_memory_usage']

        )
        step_metadata_array = np.array([step_metadata_row], dtype=step_metadata_dtype)

        # Open or restore db and append datat to it
        db = tb.open_file(
            self.db_path,
            mode='a',
            filters=self.compression_params)
        try:
            step_metadata_table = db.get_node(db.root, 'depletion_step_metadata')
        except Exception:
            step_metadata_table = db.create_table(
                db.root,
                'depletion_step_metadata',
                np.empty(0, dtype=step_metadata_dtype),
                "Depletion step metadata")

        step_metadata_table.append(step_metadata_array)
        step_metadata_table.flush()
        db.close()

    def read_k_eds_delta(self, current_timestep):
        """Reads from database delta between previous and current `keff` at the
        end of depletion step and returns `True` if predicted `keff` at the
        next depletion step drops below 1.

        Parameters
        ----------
        current_timestep : int
            Number of current depletion time step.

        Returns
        -------
        bool
            Is the reactor will become subcritical at the next step?

        """

        if current_timestep > 3 or self.restart_flag:
            # Open or restore db and read data
            db = tb.open_file(self.db_path, mode='r')
            sim_param = db.root.simulation_parameters
            k_eds = np.array([x['keff_eds'][0] for x in sim_param.iterrows()])
            db.close()
            delta_keff = np.diff(k_eds)
            avrg_keff_drop = abs(np.mean(delta_keff[-4:-1]))
            print("Average keff drop per step ", avrg_keff_drop)
            print("keff at the end of last step ", k_eds)
            if k_eds[-1] - avrg_keff_drop < 1.0:
                return True
            else:
                return False

    def check_switch_geo_trigger(self, current_time, switch_time):
        """Compares the current timestep with the user defined times
        at which to switch reactor geometry, and returns `True` if there
        is a match.

        Parameters
        ----------
        current_timestep : int
            Current time after depletion started.
        switch_time : list
            List containing moments in time when geometry have to be switched.

        Returns
        -------
        bool
            is the next geometry must be used at the next step?

        """
        if current_time in switch_time:
            return True
        else:
            return False
