from kalman_pytorch.design import Design
from kalman_pytorch.design.process import NoVelocity, ConstantVelocity, Seasonal
from kalman_pytorch.design.measurement import Measurement
from kalman_pytorch.kalman_filter import KalmanFilter

from kalman_pytorch.design.lazy_parameter import LogLinked, LogitLinked

import torch
from torch.nn import Parameter

from warnings import warn


class Forecast(KalmanFilter):
    def __init__(self,
                 variables,
                 seasonal_period,
                 level_factors='both',
                 trend_factors='common',
                 season_factors='separate'):
        """

        :param variables: A list of the names of variables being measured. Will be coerced to strings, so you can just
         pass range([number-of-variables]) if the variables don't have/need meaningful names.
        :param seasonal_period: The number of timesteps for a seasonal cycle. This is for relatively short seasonality,
        (e.g., weekly) since it increases the size of the design-matrices quadratically.
        :param level_factors: Is there a latent level that's separate for each variable, or common to all? Can pass 'separate',
        'common', 'both', or None.
        :param trend_factors: Is there a latent trend that's separate for each variable, or common to all? Can pass 'separate',
        'common', 'both', or None. If 'common' is included, it will be included in 'level' as well.
        :param season_factors: Is the latent seasonal cycle separate for each variable, or common to all? Can pass 'separate',
        'common', 'both', or None.
        """
        super(Forecast, self).__init__()

        # variables:
        self.num_variables = len(variables)
        self.variables = [str(x) for x in variables]

        # which variables go where?
        self.pos_only_vars, self.pos_and_vel_vars, self.season_vars = \
            self.classify_variables(level_factors, trend_factors, season_factors)

        # seasonal period:
        self.seasonal_period = self.parse_seasonal_arg(seasonal_period)

        # initial values ----
        self.initial_state = Parameter(torch.randn(self.num_states))
        self.initial_log_std = Parameter(torch.randn(self.num_states))

        # states ----
        self.log_core_process_std_dev = Parameter(torch.zeros(len(self.pos_or_vel_vars)))
        core_states_by_varname = dict()
        for varname in self.pos_or_vel_vars:
            process = NoVelocity if varname in self.pos_only_vars else ConstantVelocity
            idx = self.variable_to_core_param_mapper[varname]
            core_states_by_varname[varname] = process(id_prefix=varname,
                                                      std_dev=LogLinked(self.log_core_process_std_dev[idx]))

        self.log_season_process_std_dev = Parameter(torch.zeros(len(self.season_vars)))
        season_states_by_varname = dict()
        for varname in self.season_vars:
            idx = self.variable_to_season_param_mapper[varname]
            season_states_by_varname[varname] = Seasonal(id_prefix=varname,
                                                         std_dev=LogLinked(self.log_season_process_std_dev[idx]),
                                                         period=self.seasonal_period,
                                                         df_correction=varname in self.pos_or_vel_vars)

        # measurements ----
        self.log_measurement_std_dev = Parameter(torch.zeros(self.num_variables))
        all_measurements = []
        for i, varname in enumerate(self.variables):
            this_measurement = Measurement(id=varname, std_dev=LogLinked(self.log_measurement_std_dev[i]))

            for state_name in (varname, 'common'):
                core_state = core_states_by_varname.get(state_name)
                if core_state is not None:
                    this_measurement.add_state(core_state.observable)
                season_state = season_states_by_varname.get(state_name)
                if season_state is not None:
                    this_measurement.add_state(season_state.observable)

            all_measurements.append(this_measurement)

        # correlation between measurement-errors (currently constrained to be positive)
        num_corrs = ((self.num_variables + 1) * self.num_variables) / 2 - self.num_variables
        self.logit_measurement_corr = Parameter(torch.zeros(num_corrs))
        pidx = 0
        for idx1 in range(self.num_variables):
            for idx2 in range(idx1 + 1, self.num_variables):
                all_measurements[idx1].add_correlation(all_measurements[idx2],
                                                       correlation=LogitLinked(self.logit_measurement_corr[pidx]))
                pidx += 1

        # put states in the same order as variables (with common last) ---
        all_states = []
        vars_plus_common = self.variables + ['common']
        for varname in vars_plus_common:
            if varname in core_states_by_varname.keys():
                all_states.extend(core_states_by_varname[varname].states)
            if varname in season_states_by_varname.keys():
                all_states.extend(season_states_by_varname[varname].states)

        self._design = Design(states=all_states, measurements=all_measurements)

    @property
    def design(self):
        return self._design

    def initializer(self, tens):
        return self.default_initializer(tens=tens,
                                        initial_state=self.initial_state,
                                        initial_std_dev=torch.exp(self.initial_log_std))

    @property
    def num_season_states(self):
        return len(self.season_vars) * self.seasonal_period

    @property
    def num_core_states(self):
        return len(self.pos_only_vars) + 2 * len(self.pos_and_vel_vars)

    @property
    def num_states(self):
        return self.num_season_states + self.num_core_states

    def classify_variables(self, level_factors, trend_factors, season_factors):
        replacements = {'common': ('common',),
                        'separate': ('separate',),
                        'both': ('separate', 'common'),
                        None: ()}

        try:
            season = replacements[None if season_factors is None else season_factors.lower()]
            trend = replacements[None if trend_factors is None else trend_factors.lower()]
            level = replacements[None if level_factors is None else level_factors.lower()]
        except KeyError:
            raise ValueError("Unexpected values passed to season, trend and/or level; should be one of ('common', "
                             "'separate', 'both').")

        for arg in ('common', 'separate'):
            if arg in trend and arg not in level:
                warn("Since '%s' is in 'trend', it will be added to 'level'." % arg)
                level = list(level) + [arg]

        if len(season) + len(trend) + len(level) == 0:
            raise ValueError("All state arguments passed None, so this kalman-filter has no state.")

        pos_only_vars, pos_and_vel_vars, season_vars = [], [], []

        if 'separate' in trend:
            pos_and_vel_vars.extend(self.variables)
        elif 'separate' in level:
            pos_only_vars.extend(self.variables)
        if 'separate' in season:
            season_vars.extend(self.variables)

        if len(self.variables) == 1:
            warn("Univariate kalman-filter, so no 'common' factor will be used.")
        else:
            if 'common' in trend:
                pos_and_vel_vars.append('common')
            elif 'common' in level:
                pos_only_vars.append('common')
            if 'common' in season:
                season_vars.append('common')

        return tuple(pos_only_vars), tuple(pos_and_vel_vars), tuple(season_vars)

    @property
    def pos_or_vel_vars(self):
        return tuple(list(self.pos_only_vars) + list(self.pos_and_vel_vars))

    @property
    def variable_to_core_param_mapper(self):
        mapping = {}
        i = 0
        for varname in self.variables + ['common']:  # keep variable ordering and put common last
            if varname in self.pos_or_vel_vars:
                mapping[varname] = i
                i += 1
        return mapping

    @property
    def variable_to_season_param_mapper(self):
        mapping = {}
        i = 0
        for varname in self.variables + ['common']:  # keep variable ordering and put common last
            if varname in self.season_vars:
                mapping[varname] = i
                i += 1
        return mapping

    def parse_seasonal_arg(self, seasonal_period):
        seasonal_period = 0 if seasonal_period is None else seasonal_period
        num_season_vars = len(self.season_vars)
        if (num_season_vars > 0) and (seasonal_period == 0):
            raise ValueError("There are seasonal states, but you haven't indicated a (non-null) seasonal period.")
        if (num_season_vars == 0) and (seasonal_period > 0):
            raise ValueError("There are no seasonal states, but you indicated a (non-null) seasonal period.")
        return seasonal_period
