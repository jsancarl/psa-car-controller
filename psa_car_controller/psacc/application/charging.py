import logging
from datetime import datetime
from sqlite3 import IntegrityError

from typing import List

from .battery_charge_curve import BatteryChargeCurve
from .ecomix import Ecomix

from psa_car_controller.psacc.repository.config_repository import ElectricityPriceConfig
from ..model.car import Car, Cars
from ..model.charge import Charge

logger = logging.getLogger(__name__)


class Charging:
    elec_price: ElectricityPriceConfig = ElectricityPriceConfig()

    @staticmethod
    def get_chargings() -> List[dict]:
        return []

    @staticmethod
    def get_battery_curve(conn, charge, car) -> List[BatteryChargeCurve]:
        return []

    @staticmethod
    def set_charge_price(charge, conn, car):
        battery_curves = Charging.get_battery_curve(conn, charge, car)
        charge.price = Charging.elec_price.get_price(charge, battery_curves)

    @staticmethod
    def set_default_price(cars: Cars):
        logger.debug("Not saving set_default_price")

    # pylint: disable=too-many-arguments
    @staticmethod
    def update_chargings(conn, charge: Charge, car):
        Charging.set_charge_price(charge, conn, car)

    @staticmethod
    def is_charge_ended(charge: 'Charge'):
        return not charge or charge.stop_at

    @staticmethod
    def record_charging(car: Car, charging_status, charge_date: datetime, level, latitude,
                        # pylint: disable=too-many-locals,too-many-positional-arguments
                        longitude, country_code, charging_mode, charging_rate, autonomy, mileage):
        logger.debug("Not saving record_charging")

    @staticmethod
    def _calculated_fields(charge_list: list):
        for c in charge_list:
            if c.get("stop_at") and c.get("start_at"):
                c.update(
                    {
                        "duration_min": (c.get("stop_at") - c.get("start_at")).total_seconds()
                        / 60,
                        "duration_str": str((c.get("stop_at") - c.get("start_at"))),
                    }
                )
