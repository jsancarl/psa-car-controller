import logging
from typing import Dict

from geojson import FeatureCollection

from psa_car_controller.common.mylogger import CustomLogger
from psa_car_controller.psacc.application.trip_parser import TripParser

from psa_car_controller.psacc.model.trip import Trip
from psa_car_controller.psacc.model.car import Cars

logger = CustomLogger.getLogger(__name__)

MAX_SPEED = 150


class Trips(list):
    def __init__(self, *args):
        list.__init__(self, *args)
        self.trip_num = 1

    def to_geo_json(self):
        feature_collection = FeatureCollection(self)
        return feature_collection

    def get_trips_as_dict(self):
        return [trip.get_info() for trip in self]

    def get_distance(self):
        return self[-1].mileage - self[0].mileage

    def check_and_append(self, trip: Trip):
        if trip.consumption_km <= trip.car.max_elec_consumption and \
                trip.consumption_fuel_km <= trip.car.max_fuel_consumption and \
                trip.speed_average < MAX_SPEED:
            trip.id = self.trip_num
            self.trip_num += 1
            self.append(trip)
            return True
        logger.debugv("trip discarded")
        return False

    @staticmethod
    def get_speed_average(distance, duration):
        try:
            speed_average = distance / duration
        except ZeroDivisionError:
            speed_average = 0
        return speed_average

    @staticmethod
    def get_trips(vehicles_list: Cars) -> Dict[str, "Trips"]:  # noqa: MC0001
        # pylint: disable=too-many-locals,too-many-statements,too-many-nested-blocks,too-many-branches
        return {}
