# -*- coding: utf-8 -*-
"""
This is the main file of the DispaSET pre-processing tool. It comprises a single function that generated the DispaSET simulation environment.

@author: S. Quoilin
"""

import datetime as dt
import logging
import os
import shutil
import sys

import numpy as np
import pandas as pd

from .data_check import check_units, check_chp, check_heat_demand, check_df, isStorage, check_MinMaxFlows,check_AvailabilityFactors
from .utils import clustering, interconnections, incidence_matrix
from .data_handler import UnitBasedTable,NodeBasedTable,merge_series, define_parameter, write_to_excel, load_csv

from ..misc.gdx_handler import write_variables


GMS_FOLDER = os.path.join(os.path.dirname(__file__), '..', 'GAMS')




def build_simulation(config,plot_load=False):
    """
    This function reads the DispaSET config, loads the specified data,
    processes it when needed, and formats it in the proper DispaSET format.
    The output of the function is a directory with all inputs and simulation files required to run a DispaSET simulation
    
    :param config: Dictionary with all the configuration fields loaded from the excel file. Output of the 'LoadConfig' function.
    :param plot_load: Boolean used to display a plot of the demand curves in the different zones
    """
    dispa_version = str(get_git_revision_tag())
    logging.info('New build started. DispaSET version: ' + dispa_version)
    # %%################################################################################################################
    #####################################   Main Inputs    ############################################################
    ###################################################################################################################

    # Boolean variable to check wether it is milp or lp:
    LP = config['SimulationType'] == 'LP' or config['SimulationType'] == 'LP clustered'

    # Day/hour corresponding to the first and last days of the simulation:
    # Note that the first available data corresponds to 2015.01.31 (23.00) and the 
    # last day with data is 2015.12.31 (22.00)    
    y_start, m_start, d_start, _, _, _ = config['StartDate']
    y_end, m_end, d_end, _, _, _ = config['StopDate']
    config['StopDate'] = (y_end, m_end, d_end, 23, 59, 00)  # updating stopdate to the end of the day

    # Timestep
    TimeStep = '1h'

    # DispaSET technologies:
    Technologies = ['COMC', 'GTUR', 'HDAM', 'HROR', 'HPHS', 'ICEN', 'PHOT', 'STUR', 'WTOF', 'WTON', 'CAES', 'BATS',
                    'BEVS', 'THMS', 'P2GS']
    # List of renewable technologies:
    List_tech_renewables = ['WTON', 'WTOF', 'PHOT', 'HROR']
    # List of storage technologies:
    List_tech_storage = ['HDAM', 'HPHS', 'BATS', 'BEVS', 'CAES', 'THMS']
    # List of CHP types:
    List_types_CHP = ['extraction','back-pressure', 'p2h']
    # DispaSET fuels:
    Fuels = ['BIO', 'GAS', 'HRD', 'LIG', 'NUC', 'OIL', 'PEA', 'SUN', 'WAT', 'WIN', 'WST', 'OTH']

    # Indexes of the simualtion:
    idx_std = pd.DatetimeIndex(start=pd.datetime(*config['StartDate']), end=pd.datetime(*config['StopDate']),
                               freq=TimeStep)
    idx_utc_noloc = idx_std - dt.timedelta(hours=1)
    idx_utc = idx_utc_noloc.tz_localize('UTC')

    # %%#################################################################################################################
    #####################################   Data Loading    ###########################################################
    ###################################################################################################################

    # Start and end of the simulation:
    delta = idx_utc[-1] - idx_utc[0]
    days_simulation = delta.days + 1
    hours_simulation = 24 * days_simulation

    # Defining missing configuration fields for backwards compatibility:
    if not isinstance(config['default']['CostLoadShedding'],(float,int,long)):
        config['default']['CostLoadShedding'] = 1000
    if not isinstance(config['default']['CostHeatSlack'],(float,int,long)):
        config['default']['CostHeatSlack'] = 50
    
    # Load :
    Load = NodeBasedTable(config['Demand'],idx_utc_noloc,config['countries'],tablename='Demand')    
    
    if config['modifiers']['Demand'] != 1:
        logging.info('Scaling load curve by a factor ' + str(config['modifiers']['Demand']))
        Load = Load * config['modifiers']['Demand']

    # Interconnections:
    flows = load_csv(config['Interconnections'], index_col=0, parse_dates=True).fillna(0)
    NTC = load_csv(config['NTC'], index_col=0, parse_dates=True).fillna(0)

    # Load Shedding:
    LoadShedding = NodeBasedTable(config['LoadShedding'],idx_utc_noloc,config['countries'],tablename='LoadShedding',default=config['default']['LoadShedding'])    
    CostLoadShedding = NodeBasedTable(config['CostLoadShedding'],idx_utc_noloc,config['countries'],tablename='CostLoadShedding',default=config['default']['CostLoadShedding'])    

    # Power plants:
    plants = pd.DataFrame()
    if os.path.isfile(config['PowerPlantData']):
        plants = load_csv(config['PowerPlantData'])
    elif '##' in config['PowerPlantData']:
        for c in config['countries']:
            path = config['PowerPlantData'].replace('##', str(c))
            tmp = load_csv(path)
            plants = plants.append(tmp, ignore_index=True)
    plants = plants[plants['Technology'] != 'Other']
    plants = plants[pd.notnull(plants['PowerCapacity'])]
    plants.index = range(len(plants))

    # check plant list:
    check_units(config, plants)
    # If not present, add the non-compulsory fields to the units table:
    for key in ['CHPPowerLossFactor','CHPPowerToHeat','CHPType','STOCapacity','STOSelfDischarge','STOMaxChargingPower','STOChargingEfficiency', 'CHPMaxHeat']:
        if key not in plants.columns:
            plants[key] = np.nan

    # Defining the hydro storages:
    plants_sto = plants[[u in List_tech_storage for u in plants['Technology']]]
    # Defining the CHPs:
    plants_chp = plants[[str(x).lower() in List_types_CHP for x in plants['CHPType']]]

    Outages = UnitBasedTable(plants,config['Outages'],idx_utc_noloc,config['countries'],fallbacks=['Unit','Technology'],tablename='Outages')
    AF = UnitBasedTable(plants,config['RenewablesAF'],idx_utc_noloc,config['countries'],fallbacks=['Unit','Technology'],tablename='AvailabilityFactors',default=1)
    ReservoirLevels = UnitBasedTable(plants_sto,config['ReservoirLevels'],idx_utc_noloc,config['countries'],fallbacks=['Unit','Technology'],tablename='ReservoirLevels',default=0)
    ReservoirScaledInflows = UnitBasedTable(plants_sto,config['ReservoirScaledInflows'],idx_utc_noloc,config['countries'],fallbacks=['Unit','Technology'],tablename='ReservoirScaledInflows',default=0)
    HeatDemand = UnitBasedTable(plants_chp,config['HeatDemand'],idx_utc_noloc,config['countries'],fallbacks=['Unit'],tablename='HeatDemand',default=0)
    CostHeatSlack = UnitBasedTable(plants_chp,config['CostHeatSlack'],idx_utc_noloc,config['countries'],fallbacks=['Unit','Zone'],tablename='CostHeatSlack',default=config['default']['CostHeatSlack'])

    # data checks:
    check_AvailabilityFactors(plants,AF)
    check_heat_demand(plants,HeatDemand)

    # Fuel prices:
    fuels = ['PriceOfNuclear', 'PriceOfBlackCoal', 'PriceOfGas', 'PriceOfFuelOil', 'PriceOfBiomass', 'PriceOfCO2', 'PriceOfLignite', 'PriceOfPeat']
    FuelPrices = pd.DataFrame(columns=fuels, index=idx_utc_noloc)
    for fuel in fuels:
        if os.path.isfile(config[fuel]):
            tmp = load_csv(config[fuel], header=None, index_col=0, parse_dates=True)
            FuelPrices[fuel] = tmp[1][idx_utc_noloc].values
        elif isinstance(config['default'][fuel], (int, long, float, complex)):
            logging.warn('No data file found for "' + fuel + '. Using default value ' + str(config['default'][fuel]) + ' EUR')
            FuelPrices[fuel] = pd.Series(config['default'][fuel], index=idx_utc_noloc)
        # Special case for lignite and peat, for backward compatibility
        elif fuel == 'PriceOfLignite':             
            logging.warn('No price data found for "' + fuel + '. Using the same value as for Black Coal')
            FuelPrices[fuel] = FuelPrices['PriceOfBlackCoal']
        elif fuel == 'PriceOfPeat':             
            logging.warn('No price data found for "' + fuel + '. Using the same value as for biomass')
            FuelPrices[fuel] = FuelPrices['PriceOfBiomass']       
        else:
            logging.warn('No data file or default value found for "' + fuel + '. Assuming zero marginal price!')
            FuelPrices[fuel] = pd.Series(0, index=idx_utc_noloc)

    # Interconnections:
    [Interconnections_sim, Interconnections_RoW, Interconnections] = interconnections(config['countries'], NTC, flows)

    if len(Interconnections_sim.columns) > 0:
        NTCs = Interconnections_sim.loc[idx_utc_noloc, :]
    else:
        NTCs = pd.DataFrame(index=idx_utc_noloc)
    Inter_RoW = Interconnections_RoW.loc[idx_utc_noloc, :]

                    # Clustering of the plants:
    if config['SimulationType'] == 'LP clustered':
        Plants_merged, mapping = clustering(plants, method='LP')
    elif config['Clustering']:
        Plants_merged, mapping = clustering(plants, method='Standard')
    else:
        Plants_merged, mapping = clustering(plants, method=None)

    # Renaming the columns to ease the production of parameters:
    Plants_merged.rename(columns={'StartUpCost': 'CostStartUp',
                                  'RampUpMax': 'RampUpMaximum',
                                  'RampDownMax': 'RampDownMaximum',
                                  'MinUpTime': 'TimeUpMinimum',
                                  'MinDownTime': 'TimeDownMinimum',
                                  'RampingCost': 'CostRampUp',
                                  'STOCapacity': 'StorageCapacity',
                                  'STOMaxChargingPower': 'StorageChargingCapacity',
                                  'STOChargingEfficiency': 'StorageChargingEfficiency',
                                  'STOSelfDischarge': 'StorageSelfDischarge',
                                  'CO2Intensity': 'EmissionRate'}, inplace=True)
    
    for key in ['TimeUpMinimum','TimeDownMinimum']:
        if any([not x.is_integer() for x in Plants_merged[key].fillna(0).values.astype('float')]):
            logging.warn(key + ' in the power plant data has been rounded to the nearest integer value')
            Plants_merged.loc[:,key] = Plants_merged[key].fillna(0).values.astype('int32')

    if not len(Plants_merged.index.unique()) == len(Plants_merged):
        # Very unlikely case:
        logging.error('plant indexes not unique!')
        sys.exit(1)

    # Apply scaling factors:
    if config['modifiers']['Solar'] != 1:
        logging.info('Scaling Solar Capacity by a factor ' + str(config['modifiers']['Solar']))
        for u in Plants_merged.index:
            if Plants_merged.Technology[u] == 'PHOT':
                Plants_merged.loc[u, 'PowerCapacity'] = Plants_merged.loc[u, 'PowerCapacity'] * config['modifiers']['Solar']
    if config['modifiers']['Wind'] != 1:
        logging.info('Scaling Wind Capacity by a factor ' + str(config['modifiers']['Wind']))
        for u in Plants_merged.index:
            if Plants_merged.Technology[u] == 'WTON' or Plants_merged.Technology[u] == 'WTOF':
                Plants_merged.loc[u, 'PowerCapacity'] = Plants_merged.loc[u, 'PowerCapacity'] * config['modifiers']['Wind']
    if config['modifiers']['Storage'] != 1:
        logging.info('Scaling Storage Power and Capacity by a factor ' + str(config['modifiers']['Storage']))
        for u in Plants_merged.index:
            if isStorage(Plants_merged.Technology[u]):
                Plants_merged.loc[u, 'PowerCapacity'] = Plants_merged.loc[u, 'PowerCapacity'] * config['modifiers']['Storage']
                Plants_merged.loc[u, 'StorageCapacity'] = Plants_merged.loc[u, 'StorageCapacity'] * config['modifiers']['Storage']
                Plants_merged.loc[u, 'StorageChargingCapacity'] = Plants_merged.loc[u, 'StorageChargingCapacity'] * config['modifiers']['Storage']

    # Defining the hydro storages:
    Plants_sto = Plants_merged[[u in List_tech_storage for u in Plants_merged['Technology']]]
    # Defining the CHPs:
    Plants_chp = Plants_merged[[x.lower() in List_types_CHP for x in Plants_merged['CHPType']]].copy()
    # check chp plants:
    check_chp(config, Plants_chp)
    # For all the chp plants correct the PowerCapacity, which is defined in cogeneration mode in the inputs and in power generation model in the optimization model
    for u in Plants_chp.index:
        PowerCapacity = Plants_chp.loc[u, 'PowerCapacity']

        if Plants_chp.loc[u,'CHPType'].lower() == 'p2h':
            PurePowerCapacity = PowerCapacity
        else:
            if pd.isnull(Plants_chp.loc[u,'CHPMaxHeat']):  # If maximum heat is not defined, then it is defined as the intersection between two lines
                MaxHeat = PowerCapacity / Plants_chp.loc[u,'CHPPowerToHeat']
                Plants_chp.loc[u, 'CHPMaxHeat'] = 'inf'
            else:
                MaxHeat = Plants_chp.loc[u, 'CHPMaxHeat']
            PurePowerCapacity = PowerCapacity + Plants_chp.loc[u,'CHPPowerLossFactor'] * MaxHeat
        Plants_merged.loc[u,'PartLoadMin'] = Plants_merged.loc[u,'PartLoadMin'] * PowerCapacity / PurePowerCapacity  # FIXME: Is this correct?
        Plants_merged.loc[u,'PowerCapacity'] = PurePowerCapacity
        
    # Get the hydro time series corresponding to the original plant list:
    StorageFormerIndexes = [s for s in plants.index if
                            plants['Technology'][s] in List_tech_storage]

    # Same with the CHPs:
    # Get the heat demand time series corresponding to the original plant list:
    CHPFormerIndexes = [s for s in plants.index if
                            plants['CHPType'][s] in List_types_CHP]
    for s in CHPFormerIndexes:  # for all the old plant indexes
        # get the old plant name corresponding to s:
        oldname = plants['Unit'][s]
        newname = mapping['NewIndex'][s]
        if oldname not in HeatDemand:
            logging.warn('No heat demand profile found for CHP plant "' + str(oldname) + '". Assuming zero')
            HeatDemand[oldname] = 0
        if oldname not in CostHeatSlack:
            logging.warn('No heat cost profile found for CHP plant "' + str(oldname) + '". Assuming zero')
            CostHeatSlack[oldname] = 0
 

    # merge the outages:
    for i in plants.index:  # for all the old plant indexes
        # get the old plant name corresponding to s:
        oldname = plants['Unit'][i]
        newname = mapping['NewIndex'][i]

    # Merging the time series relative to the clustered power plants:
    ReservoirScaledInflows_merged = merge_series(plants, ReservoirScaledInflows, mapping, method='WeightedAverage', tablename='ScaledInflows')
    ReservoirLevels_merged = merge_series(plants, ReservoirLevels, mapping, tablename='ReservoirLevels')
    Outages_merged = merge_series(plants, Outages, mapping, tablename='Outages')
    HeatDemand_merged = merge_series(plants, HeatDemand, mapping, tablename='HeatDemand',method='Sum')
    AF_merged = merge_series(plants, AF, mapping, tablename='AvailabilityFactors')
    CostHeatSlack_merged = merge_series(plants, CostHeatSlack, mapping, tablename='CostHeatSlack')
    
    # Correcting heat demands in case of outages:
    for key in HeatDemand_merged:
        if key in Outages_merged and Outages_merged[key].max() > 0:
            HeatDemand_merged[key] = HeatDemand_merged[key].values * (1 - Outages_merged[key].values)
            logging.info('Corrected the heat demand profile for chp unit ' + key + ' with the outage values')

    # %%
    # checking data
    check_df(Load, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1], name='Load')
    check_df(AF_merged, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1],
             name='AF_merged')
    check_df(Outages_merged, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1], name='Outages_merged')
    check_df(Inter_RoW, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1], name='Inter_RoW')
    check_df(FuelPrices, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1], name='FuelPrices')
    check_df(NTCs, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1], name='NTCs')
    check_df(ReservoirLevels_merged, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1],
             name='ReservoirLevels_merged')
    check_df(ReservoirScaledInflows_merged, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1],
             name='ReservoirScaledInflows_merged')
    check_df(HeatDemand_merged, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1],
             name='HeatDemand_merged')
    check_df(CostHeatSlack_merged, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1],
             name='CostHeatSlack_merged')
    check_df(LoadShedding, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1],
             name='LoadShedding')
    check_df(CostLoadShedding, StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1],
             name='CostLoadShedding')

#    for key in Renewables:
#        check_df(Renewables[key], StartDate=idx_utc_noloc[0], StopDate=idx_utc_noloc[-1],
#                 name='Renewables["' + key + '"]')

    # %%%

    # Extending the data to include the look-ahead period (with constant values assumed)
    enddate_long = idx_utc_noloc[-1] + dt.timedelta(days=config['LookAhead'])
    idx_long = pd.DatetimeIndex(start=idx_utc_noloc[0], end=enddate_long, freq=TimeStep)
    Nhours_long = len(idx_long)

    # re-indexing with the longer index and filling possibly missing data at the beginning and at the end::
    Load = Load.reindex(idx_long, method='nearest').fillna(method='bfill')
    AF_merged = AF_merged.reindex(idx_long, method='nearest').fillna(method='bfill')
    Inter_RoW = Inter_RoW.reindex(idx_long, method='nearest').fillna(method='bfill')
    NTCs = NTCs.reindex(idx_long, method='nearest').fillna(method='bfill')
    FuelPrices = FuelPrices.reindex(idx_long, method='nearest').fillna(method='bfill')
    Load = Load.reindex(idx_long, method='nearest').fillna(method='bfill')
    Outages_merged = Outages_merged.reindex(idx_long, method='nearest').fillna(method='bfill')
    ReservoirLevels_merged = ReservoirLevels_merged.reindex(idx_long, method='nearest').fillna(method='bfill')
    ReservoirScaledInflows_merged = ReservoirScaledInflows_merged.reindex(idx_long, method='nearest').fillna(
        method='bfill')
    LoadShedding = LoadShedding.reindex(idx_long, method='nearest').fillna(method='bfill')
    CostLoadShedding = CostLoadShedding.reindex(idx_long, method='nearest').fillna(method='bfill')
#    for tr in Renewables:
#        Renewables[tr] = Renewables[tr].reindex(idx_long, method='nearest').fillna(method='bfill')

    # %%################################################################################################################
    ############################################   Sets    ############################################################
    ###################################################################################################################

    # The sets are defined within a dictionnary:
    sets = {}
    sets['h'] = [str(x + 1) for x in range(Nhours_long)]
    sets['z'] = [str(x + 1) for x in range(Nhours_long - config['LookAhead'] * 24)]
    sets['mk'] = ['DA', '2U', '2D']
    sets['n'] = config['countries']
    sets['u'] = Plants_merged.index.tolist()
    sets['l'] = Interconnections
    sets['f'] = Fuels
    sets['p'] = ['CO2']
    sets['s'] = Plants_sto.index.tolist()
    sets['chp'] = Plants_chp.index.tolist()
    sets['t'] = Technologies
    sets['tr'] = List_tech_renewables

    ###################################################################################################################
    ############################################   Parameters    ######################################################
    ###################################################################################################################

    Nunits = len(Plants_merged)
    parameters = {}

    # Each parameter is associated with certain sets, as defined in the following list:
    sets_param = {}
    sets_param['AvailabilityFactor'] = ['u', 'h']
    sets_param['CHPPowerToHeat'] = ['chp']
    sets_param['CHPPowerLossFactor'] = ['chp']
    sets_param['CHPMaxHeat'] = ['chp']
    sets_param['CostFixed'] = ['u']
    sets_param['CostHeatSlack'] = ['chp','h']
    sets_param['CostLoadShedding'] = ['n','h']
    sets_param['CostRampUp'] = ['u']
    sets_param['CostRampDown'] = ['u']
    sets_param['CostShutDown'] = ['u']
    sets_param['CostStartUp'] = ['u']
    sets_param['CostVariable'] = ['u', 'h']
    sets_param['Curtailment'] = ['n']
    sets_param['Demand'] = ['mk', 'n', 'h']
    sets_param['Efficiency'] = ['u']
    sets_param['EmissionMaximum'] = ['n', 'p']
    sets_param['EmissionRate'] = ['u', 'p']
    sets_param['FlowMaximum'] = ['l', 'h']
    sets_param['FlowMinimum'] = ['l', 'h']
    sets_param['FuelPrice'] = ['n', 'f', 'h']
    sets_param['Fuel'] = ['u', 'f']
    sets_param['HeatDemand'] = ['chp','h']
    sets_param['LineNode'] = ['l', 'n']
    sets_param['LoadShedding'] = ['n','h']
    sets_param['Location'] = ['u', 'n']
    sets_param['Markup'] = ['u', 'h']
    sets_param['Nunits'] = ['u']
    sets_param['OutageFactor'] = ['u', 'h']
    sets_param['PartLoadMin'] = ['u']
    sets_param['PowerCapacity'] = ['u']
    sets_param['PowerInitial'] = ['u']
    sets_param['PriceTransmission'] = ['l', 'h']
    sets_param['RampUpMaximum'] = ['u']
    sets_param['RampDownMaximum'] = ['u']
    sets_param['RampStartUpMaximum'] = ['u']
    sets_param['RampShutDownMaximum'] = ['u']
    sets_param['Reserve'] = ['t']
    sets_param['StorageCapacity'] = ['u']            
    sets_param['StorageChargingCapacity'] = ['s']
    sets_param['StorageChargingEfficiency'] = ['s']
    sets_param['StorageDischargeEfficiency'] = ['s']
    sets_param['StorageSelfDischarge'] = ['u']
    sets_param['StorageInflow'] = ['s', 'h']
    sets_param['StorageInitial'] = ['s']
    sets_param['StorageMinimum'] = ['s']
    sets_param['StorageOutflow'] = ['s', 'h']
    sets_param['StorageProfile'] = ['s', 'h']
    sets_param['Technology'] = ['u', 't']
    sets_param['TimeUpMinimum'] = ['u']
    sets_param['TimeDownMinimum'] = ['u']
    sets_param['TimeUpInitial'] = ['u']
    sets_param['TimeDownInitial'] = ['u']

    # Define all the parameters and set a default value of zero:
    for var in sets_param:
        parameters[var] = define_parameter(sets_param[var], sets, value=0)

    # List of parameters whose default value is 1
    for var in ['AvailabilityFactor', 'Efficiency', 'Curtailment', 'StorageChargingEfficiency',
                'StorageDischargeEfficiency', 'Nunits']:
        parameters[var] = define_parameter(sets_param[var], sets, value=1)

    # List of parameters whose default value is very high
    for var in ['RampUpMaximum', 'RampDownMaximum', 'RampStartUpMaximum', 'RampShutDownMaximum', 'EmissionMaximum',
                'TimeUpInitial', 'TimeDownInitial']:
        parameters[var] = define_parameter(sets_param[var], sets, value=1e7)

    # Boolean parameters:
    for var in ['Technology', 'Fuel', 'Reserve', 'Location']:
        parameters[var] = define_parameter(sets_param[var], sets, value='bool')

    # %%
    # List of parameters whose value is known, and provided in the dataframe Plants_merged.
    for var in ['Efficiency', 'PowerCapacity', 'PartLoadMin', 'TimeUpMinimum', 'TimeDownMinimum', 'CostStartUp',
                'CostRampUp','StorageCapacity', 'StorageSelfDischarge']:
        parameters[var]['val'] = Plants_merged[var].values

    # List of parameters whose value is not necessarily specified in the dataframe Plants_merged
    for var in ['Nunits']:
        if var in Plants_merged:
            parameters[var]['val'] = Plants_merged[var].values


    # List of parameters whose value is known, and provided in the dataframe Plants_sto.
    for var in ['StorageChargingCapacity', 'StorageChargingEfficiency']:
        parameters[var]['val'] = Plants_sto[var].values

    # The storage discharge efficiency is actually given by the unit efficiency:
    parameters['StorageDischargeEfficiency']['val'] = Plants_sto['Efficiency'].values
    
    # List of parameters whose value is known, and provided in the dataframe Plants_chp
    for var in ['CHPPowerToHeat','CHPPowerLossFactor', 'CHPMaxHeat']:
        parameters[var]['val'] = Plants_chp[var].values

    # Storage profile and initial state:
    for i, s in enumerate(sets['s']):
        if s in ReservoirLevels_merged:
            # get the time
            parameters['StorageInitial']['val'][i] = ReservoirLevels_merged[s][idx_long[0]] * \
                                                     Plants_sto['StorageCapacity'][s]
            parameters['StorageProfile']['val'][i, :] = ReservoirLevels_merged[s][idx_long].values
            if any(ReservoirLevels_merged[s] > 1):
                logging.warn(s + ': The reservoir level is sometimes higher than its capacity!')
        else:
            logging.warn( 'Could not find reservoir level data for storage plant ' + s + '. Assuming 50% of capacity')
            parameters['StorageInitial']['val'][i] = 0.5 * Plants_sto['StorageCapacity'][s]
            parameters['StorageProfile']['val'][i, :] = 0.5

    # Storage Inflows:
    for i, s in enumerate(sets['s']):
        if s in ReservoirScaledInflows_merged:
            parameters['StorageInflow']['val'][i, :] = ReservoirScaledInflows_merged[s][idx_long].values * \
                                                       Plants_sto['PowerCapacity'][s]
    # CHP time series:
    for i, u in enumerate(sets['chp']):
        if u in HeatDemand_merged:
            parameters['HeatDemand']['val'][i, :] = HeatDemand_merged[u][idx_long].values 
            parameters['CostHeatSlack']['val'][i, :] = CostHeatSlack_merged[u][idx_long].values

    # Ramping rates are reconstructed for the non dimensional value provided (start-up and normal ramping are not differentiated)
    parameters['RampUpMaximum']['val'] = Plants_merged['RampUpRate'].values * Plants_merged['PowerCapacity'].values * 60
    parameters['RampDownMaximum']['val'] = Plants_merged['RampDownRate'].values * Plants_merged[
        'PowerCapacity'].values * 60
    parameters['RampStartUpMaximum']['val'] = Plants_merged['RampUpRate'].values * Plants_merged[
        'PowerCapacity'].values * 60
    parameters['RampShutDownMaximum']['val'] = Plants_merged['RampDownRate'].values * Plants_merged[
        'PowerCapacity'].values * 60

    # If Curtailment is not allowed, set to 0:
    if config['AllowCurtailment'] == 0:
        parameters['Curtailment'] = define_parameter(sets_param['Curtailment'], sets, value=0)

    # Availability Factors
    if len(AF_merged.columns) != 0:
        for i, u in enumerate(sets['u']):
            if u in AF_merged.columns:
                parameters['AvailabilityFactor']['val'][i, :] = AF_merged[u].values


    # Demand
    # Dayahead['NL'][1800:1896] = Dayahead['NL'][1632:1728]
    reserve_2U_tot = {i: (np.sqrt(10 * max(Load[i]) + 150 ** 2) - 150) for i in Load.columns}
    reserve_2D_tot = {i: (0.5 * reserve_2U_tot[i]) for i in Load.columns}

    values = np.ndarray([len(sets['mk']), len(sets['n']), len(sets['h'])])
    for i in range(len(sets['n'])):
        values[0, i, :] = Load[sets['n'][i]]
        values[1, i, :] = reserve_2U_tot[sets['n'][i]]
        values[2, i, :] = reserve_2D_tot[sets['n'][i]]
    parameters['Demand'] = {'sets': sets_param['Demand'], 'val': values}

    # Emission Rate:
    parameters['EmissionRate']['val'][:, 0] = Plants_merged['EmissionRate'].values

    # Load Shedding:
    for i, c in enumerate(sets['n']):
        parameters['LoadShedding']['val'][i] = LoadShedding[c] * Load[c].max()
        parameters['CostLoadShedding']['val'][i] = CostLoadShedding[c]

    # %%#################################################################################################################################################################################################
    # Variable Cost
    # Equivalence dictionnary between fuel types and price entries in the config sheet:
    FuelEntries = {'BIO':'PriceOfBiomass', 'GAS':'PriceOfGas', 'HRD':'PriceOfBlackCoal', 'LIG':'PriceOfLignite', 'NUC':'PriceOfNuclear', 'OIL':'PriceOfFuelOil', 'PEA':'PriceOfPeat'}
    for unit in range(Nunits):
        found = False
        for FuelEntry in FuelEntries:            
            if Plants_merged['Fuel'][unit] == FuelEntry:
                parameters['CostVariable']['val'][unit, :] = FuelPrices[FuelEntries[FuelEntry]] / Plants_merged['Efficiency'][unit] + \
                                                             Plants_merged['EmissionRate'][unit] * FuelPrices['PriceOfCO2']
                found = True
        # Special case for biomass plants, which are not included in EU ETS:
        if Plants_merged['Fuel'][unit] == 'BIO':
            parameters['CostVariable']['val'][unit, :] = FuelPrices['PriceOfBiomass'] / Plants_merged['Efficiency'][
                unit]  
            found = True
        if not found:
            logging.warn('No fuel price value has been found for fuel ' + Plants_merged['Fuel'][unit] + ' in unit ' + \
                  Plants_merged['Unit'][unit] + '. A null variable cost has been assigned')

    # %%#################################################################################################################################################################################################

    # Maximum Line Capacity
    for i, l in enumerate(sets['l']):
        if l in NTCs.columns:
            parameters['FlowMaximum']['val'][i, :] = NTCs[l]
        if l in Inter_RoW.columns:
            parameters['FlowMaximum']['val'][i, :] = Inter_RoW[l]
            parameters['FlowMinimum']['val'][i, :] = Inter_RoW[l]
    # Check values:
    check_MinMaxFlows(parameters['FlowMinimum']['val'],parameters['FlowMaximum']['val'])
    
    parameters['LineNode'] = incidence_matrix(sets, 'l', parameters, 'LineNode')

    # Outage Factors
    if len(Outages_merged.columns) != 0:
        for i, u in enumerate(sets['u']):
            if u in Outages_merged.columns:
                parameters['OutageFactor']['val'][i, :] = Outages_merged[u].values
            else:
                logging.warn('Outages factors not found for unit ' + u + '. Assuming no outages')

    # Participation to the reserve market
    values = np.array([s in config['ReserveParticipation'] for s in sets['t']], dtype='bool')
    parameters['Reserve'] = {'sets': sets_param['Reserve'], 'val': values}

    # Technologies
    for unit in range(Nunits):
        idx = sets['t'].index(Plants_merged['Technology'][unit])
        parameters['Technology']['val'][unit, idx] = True

        # Fuels
    for unit in range(Nunits):
        idx = sets['f'].index(Plants_merged['Fuel'][unit])
        parameters['Fuel']['val'][unit, idx] = True

        # Location
    for i in range(len(sets['n'])):
        parameters['Location']['val'][:, i] = (Plants_merged['Zone'] == config['countries'][i]).values

    # CHPType parameter:
    sets['chp_type'] = ['Extraction','Back-Pressure', 'P2H']
    parameters['CHPType'] = define_parameter(['chp','chp_type'],sets,value=0)
    for i,u in enumerate(sets['chp']):
        if u in Plants_chp.index:
            if Plants_chp.loc[u,'CHPType'].lower() == 'extraction':
                parameters['CHPType']['val'][i,0] = 1
            elif Plants_chp.loc[u,'CHPType'].lower() == 'back-pressure':
                parameters['CHPType']['val'][i,1] = 1
            elif Plants_chp.loc[u,'CHPType'].lower() == 'p2h':
                parameters['CHPType']['val'][i,2] = 1
            else:
                logging.error('CHPType not valid for plant ' + u)
                sys.exit(1)

    # Initial Power
    if 'InitialPower' in Plants_merged:
        parameters['PowerInitial']['val'] = Plants_merged['InitialPower'].values
    else:
        for i in range(Nunits):
            # Nuclear and Fossil Gas greater than 350 MW are up (assumption):
            if Plants_merged['Fuel'][i] in ['GAS', 'NUC'] and Plants_merged['PowerCapacity'][i] > 350:
                parameters['PowerInitial']['val'][i] = (Plants_merged['PartLoadMin'][i] + 1) / 2 * \
                                                       Plants_merged['PowerCapacity'][i]
            # Config variables:
    sets['x_config'] = ['FirstDay', 'LastDay', 'RollingHorizon Length', 'RollingHorizon LookAhead']
    sets['y_config'] = ['year', 'month', 'day']
    dd_begin = idx_long[4]
    dd_end = idx_long[-2]

    values = np.array([
        [dd_begin.year, dd_begin.month, dd_begin.day],
        [dd_end.year, dd_end.month, dd_end.day],
        [0, 0, config['HorizonLength']],
        [0, 0, config['LookAhead']]
    ])
    parameters['Config'] = {'sets': ['x_config', 'y_config'], 'val': values}

    # %%#################################################################################################################
    ######################################   Simulation Environment     ################################################
    ####################################################################################################################

    # Output folder: 
    sim = config['SimulationDirectory']

    # Simulation data:
    SimData = {'sets': sets, 'parameters': parameters, 'config': config, 'units': Plants_merged, 'version': dispa_version}

    # list_vars = []
    gdx_out = "Inputs.gdx"
    if config['WriteGDX']:
        write_variables(config['GAMS_folder'], gdx_out, [sets, parameters])

    # if the sim variable was not defined:
    if 'sim' not in locals():
        logging.error('Please provide a path where to store the DispaSET inputs (in the "sim" variable)')
        sys.exit(1)

    if not os.path.exists(sim):
        os.makedirs(sim)
    if LP:
        fin = open(os.path.join(GMS_FOLDER, 'UCM_h.gms'))
        fout = open(os.path.join(sim,'UCM_h.gms'), "wt")
        for line in fin:
            fout.write(line.replace('$setglobal LPFormulation 0', '$setglobal LPFormulation 1'))
        fin.close()
        fout.close()
    else:
        shutil.copyfile(os.path.join(GMS_FOLDER, 'UCM_h.gms'),
                        os.path.join(sim, 'UCM_h.gms'))
    gmsfile = open(os.path.join(sim, 'UCM.gpr'), 'w')
    gmsfile.write(
        '[PROJECT] \n \n[RP:UCM_H] \n1= \n[OPENWINDOW_1] \nFILE0=UCM_h.gms \nFILE1=UCM_h.gms \nMAXIM=1 \nTOP=50 \nLEFT=50 \nHEIGHT=400 \nWIDTH=400')
    gmsfile.close()
    shutil.copyfile(os.path.join(GMS_FOLDER, 'writeresults.gms'),
                    os.path.join(sim, 'writeresults.gms'))
    logging.debug('Using gams file from ' + GMS_FOLDER)
    if config['WriteGDX']:
        shutil.copy(gdx_out, sim + '/')
        os.remove(gdx_out)
    # Copy bat file to generate gdx file directly from excel:
    shutil.copy(os.path.join(GMS_FOLDER, 'makeGDX.bat'),
                os.path.join(sim, 'makeGDX.bat'))

    if config['WriteExcel']:
        write_to_excel(sim, [sets, parameters])

    if config['WritePickle']:
        import cPickle
        with open(os.path.join(sim, 'Inputs.p'), 'wb') as pfile:
            cPickle.dump(SimData, pfile, protocol=cPickle.HIGHEST_PROTOCOL)
    logging.info('Build finished')
    
    if os.path.isfile('warn.log'):
        shutil.copy('warn.log', os.path.join(sim, 'warn_preprocessing.log'))
    # %%################################################################################################################
    #####################################   Plotting load and VRE      ################################################
    ###################################################################################################################

    if plot_load:
        import matplotlib.pyplot as plt
        fig = plt.figure()
        ax1 = fig.add_subplot(111)
    
        # Plotting the 15-min load data for a visual check:
        N = len(Load[config['countries']])
        for i in config['countries']:
            ax1.plot(Load[i].resample('h').mean(), label='Load ' + i)
    
        x_ticks = np.linspace(0, N - 1, 20, dtype='int32')
        plt.xticks(rotation='vertical')
        plt.ylabel('Power (MW)')
        fig.subplots_adjust(bottom=0.2)
    
        ax1.legend()
        # plt.show() # Removed it for now because it caused problems to headless boxes
        logging.debug('Plotted 15-min load data in ' + sim + '/ALL_YEAR.pdf')
    
        fig.set_size_inches(15, 12)
        fig.savefig(sim + '/ALL_YEAR.pdf', dpi=300, bbox_inches='tight')

    return SimData

def get_git_revision_tag():
    """Get version of DispaSET used for this run. tag + commit hash"""
    from subprocess import check_output
    try:
        return check_output(["git", "describe"]).strip()
    except:
        return 'NA'
