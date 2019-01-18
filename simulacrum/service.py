#!/usr/bin/env python3
import asyncio

from caproto import (ChannelString, ChannelEnum, ChannelDouble,
                     ChannelChar, ChannelData, ChannelInteger,
                     ChannelType)
from .route_channel import (StringRoute, EnumRoute, DoubleRoute,
                           CharRoute, IntegerRoute, BoolRoute,
                           ByteRoute, ShortRoute, BoolRoute)
import re
import bpm_sim.bpm as bpm

route_type_map = {
    str: CharRoute,
    bytes: ByteRoute,
    int: IntegerRoute,
    float: DoubleRoute,
    bool: BoolRoute,

    ChannelType.STRING: StringRoute,
    ChannelType.INT: ShortRoute,
    ChannelType.LONG: IntegerRoute,
    ChannelType.DOUBLE: DoubleRoute,
    ChannelType.ENUM: EnumRoute,
    ChannelType.CHAR: CharRoute,
}

default_values = {
    str: '',
    int: 0,
    float: 0.0,
    bool: False,

    ChannelType.STRING: '',
    ChannelType.INT: 0,
    ChannelType.LONG: 0,
    ChannelType.DOUBLE: 0.0,
    ChannelType.ENUM: 0,
    ChannelType.CHAR: '',
}

class Service(dict):
    def __init__(self):
        super().__init__()
        self.routes = []
        
    def add_route(self, pattern, data_type, get, put=None, new_subscription=None, remove_subscription=None):
        self.routes.append((re.compile(pattern), data_type, get, put, new_subscription, remove_subscription))
    
    def __getitem__(self, pvname):
        chan = None
        for (pattern, data_type, get_route, put_route, new_subscription_route, remove_subscription_route) in self.routes:
            print("Testing {} against {}".format(pvname, pattern.pattern))
            if pattern.match(pvname) != None:
                chan = self.make_route_channel(pvname, data_type, get_route, put_route, new_subscription_route, remove_subscription_route)
        if chan is None:
            raise KeyError
        ret = self[pvname] = chan
        return ret
    
    def __contains__(self, key):
        for (pattern, data_type, get_route, put_route, new_subscription_route, remove_subscription_route) in self.routes:
            if pattern.match(pvname) != None:
                return True
        return False
    
    def make_route_channel(self, pvname, data_type, getter, setter=None, new_subscription=None, remove_subscription=None):
        if data_type in route_type_map:
            route_class = route_type_map[data_type]
            return route_class(pvname, getter, setter, new_subscription, remove_subscription, value=default_values[data_type])
        else:
            raise ValueError("Router doesn't know what EPICS type to use for Python type {}".format(data_type))