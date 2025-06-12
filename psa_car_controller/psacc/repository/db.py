import logging
# import sqlite3  # Eliminado
import mariadb
from datetime import datetime
from threading import Lock
from time import sleep

from typing import Callable, List
import pytz
import requests

from geojson import Feature, Point, FeatureCollection
from geojson import dumps as geo_dumps

from psa_car_controller.common import utils
from psa_car_controller.psacc.model.battery_curve import BatteryCurveDto
from psa_car_controller.psacc.model.battery_soh import BatterySoh
from psa_car_controller.psacc.model.charge import Charge
from psa_car_controller.psacc.utils.utils import get_temp

logger = logging.getLogger(__name__)

NEW_BATTERY_COLUMNS = [["price", "INTEGER"], ["charging_mode", "TEXT"], ["mileage", "REAL"]]
NEW_POSITION_COLUMNS = [["level_fuel", "INTEGER"], ["altitude", "INTEGER"]]
NEW_BATTERY_CURVE_COLUMNS = [["rate", "INTEGER"], ["autonomy", "INTEGER"]]


def convert_sql_res(rows):
    return list(map(dict, rows))


DATE_FORMAT = "%Y-%m-%d %H:%M:%S+00:00"


def dict_key_to_lower_case(**kwargs) -> dict:
    return dict((k.lower(), v) for k, v in kwargs.items())


class CustomMariaDBConnection:
    def __init__(self, *args, **kwargs):
        self.callbacks = []
    def cursor(self, dictionary=False):
        return self.conn.cursor(dictionary=dictionary)
    def commit(self):
        self.conn.commit()
    def close(self):
        if self.conn:
            self.conn.close()
    def execute_callbacks(self):
        for callback in self.callbacks:
            callback()
    @property
    def is_connected(self):
        return self.conn is not None and self.conn.open


class Database:
    callback_fct: Callable[[], None] = lambda: None
    # Configuración de conexión MariaDB
    DB_CONFIG = {
        'host': 'localhost',
        'user': 'root',
        'password': 'password',
        'database': 'psa_car_controller',
        'autocommit': True
    }
    db_initialized = False
    __thread_lock = Lock()

    @staticmethod
    def convert_datetime_from_string(string):
        return datetime.fromisoformat(string)

    @staticmethod
    def convert_datetime_from_bytes(bytes_string):
        return Database.convert_datetime_from_string(bytes_string.decode("utf-8"))

    @staticmethod
    def convert_datetime_to_string(date: datetime):
        return date.replace(tzinfo=pytz.UTC).isoformat(timespec='seconds', sep=" ")

    @staticmethod
    def set_db_callback(callbackfct):
        Database.callback_fct = callbackfct

    @staticmethod
    def backup(conn):
        # MariaDB: el backup se hace diferente, aquí solo placeholder
        logger.info("Backup not implemented for MariaDB in this script.")

    @staticmethod
    def init_db(conn):
        new_db = True
        try:
            cursor = conn.cursor()
            cursor.execute("""CREATE TABLE IF NOT EXISTS position (Timestamp DATETIME PRIMARY KEY,
                                                                     VIN VARCHAR(32), longitude DOUBLE,
                                                                     latitude DOUBLE,
                                                                     mileage DOUBLE,
                                                                     level INT,
                                                                     level_fuel INT,
                                                                     moving BOOLEAN,
                                                                     temperature INT,
                                                                     altitude INT);""")
        except mariadb.Error as e:
            new_db = False
            logger.debug(f"Database already exist or error: {e}")
        make_backup = False
        cursor.execute("""CREATE TABLE IF NOT EXISTS battery (start_at DATETIME PRIMARY KEY,stop_at DATETIME,VIN VARCHAR(32), 
                         start_level INT, end_level INT, co2 INT, kw INT, price INT, charging_mode VARCHAR(32), mileage DOUBLE);""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS battery_curve (start_at DATETIME, VIN VARCHAR(32), date DATETIME,
                        level INT, rate INT, autonomy INT, UNIQUE KEY unique_curve (start_at, VIN, level));""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS
                        battery_soh(date DATETIME, VIN VARCHAR(32), level FLOAT, UNIQUE KEY unique_soh (VIN, level));""")
        # ALTER TABLE para nuevas columnas
        table_to_update = [["position", NEW_POSITION_COLUMNS],
                           ["battery", NEW_BATTERY_COLUMNS],
                           ["battery_curve", NEW_BATTERY_CURVE_COLUMNS]]
        for table, columns in table_to_update:
            for column, column_type in columns:
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type};")
                    make_backup = True
                except mariadb.Error:
                    pass
        if not new_db and make_backup:
            Database.backup(conn)
        # Triggers y PRAGMA no aplican en MariaDB
        Database.clean_battery(conn)
        Database.add_altitude_to_db(conn)
        conn.commit()
        Database.db_initialized = True

    @staticmethod
    def check_db_access() -> bool:
        try:
            Database.get_db()
            return True
        except mariadb.Error:
            logger.fatal("Can't access to db file check permission")
        return False

    @staticmethod
    def get_db(db_config=None, update_callback=True) -> CustomMariaDBConnection:
        if db_config is None:
            db_config = Database.DB_CONFIG
        conn = CustomMariaDBConnection(**db_config)
        with Database.__thread_lock:
            if not Database.db_initialized:
                Database.init_db(conn)
        if update_callback:
            conn.callbacks.append(Database.callback_fct)
        return conn

    @staticmethod
    def clean_battery(conn):
        cursor = conn.cursor()
        cursor.execute("DELETE FROM battery WHERE start_level >= end_level-1;")
        conn.commit()

    @staticmethod
    def clean_position(conn):
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT Timestamp,mileage,level from position ORDER BY Timestamp DESC LIMIT 3;")
        res = cursor.fetchall()
        if len(res) == 3 and res[0]["mileage"] == res[1]["mileage"] == res[2]["mileage"] and \
                res[0]["level"] == res[1]["level"] == res[2]["level"]:
            logger.debug("Delete duplicate line")
            cursor.execute("DELETE FROM position where Timestamp=%s;", (res[1]["Timestamp"],))
            conn.commit()

    @staticmethod
    def get_last_temp(vin):
        conn = Database.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT temperature FROM position WHERE VIN=%s ORDER BY Timestamp DESC limit 1", (vin,))
        res = cursor.fetchone()
        conn.close()
        if res is None:
            return None
        return res[0]

    @staticmethod
    def set_chargings_price(conn, charge: Charge):
        cursor = conn.cursor()
        update = cursor.execute("UPDATE battery SET price=%s WHERE start_at=%s AND VIN=%s",
                              (charge.price, charge.start_at, charge.vin))
        conn.commit()
        if cursor.rowcount == 0:
            logger.error("Can't find line to update in the database")
        return cursor.rowcount

    @staticmethod
    def get_battery_curve(conn, start_at, stop_at, vin):
        battery_curves = []
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""SELECT date, level, rate, autonomy
                                                FROM battery_curve
                                                WHERE start_at=%s and date<=%s and VIN=%s
                                                ORDER BY date asc;""",
                           (start_at, stop_at, vin))
        res = cursor.fetchall()
        for row in res:
            battery_curves.append(BatteryCurveDto(**dict_key_to_lower_case(**row)))
        return battery_curves

    @staticmethod
    def add_altitude_to_db(conn):
        max_pos_by_req = 100
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(1) FROM position WHERE altitude IS NULL "
                               "and longitude IS NOT NULL AND latitude IS NOT NULL;")
        nb_null = cursor.fetchone()[0]
        if nb_null > max_pos_by_req:
            logger.warning("There is %s to fetch from API, it can take some time", nb_null)
        try:
            while True:
                cursor.execute("SELECT DISTINCT latitude,longitude FROM position WHERE altitude IS NULL "
                                   "and longitude IS NOT NULL AND latitude IS NOT NULL LIMIT %s;",
                                   (max_pos_by_req,))
                res = cursor.fetchall()
                nb_res = len(res)
                if nb_res > 0:
                    logger.debug("add altitude for %s positions point", nb_null)
                    nb_null -= nb_res
                    data = utils.get_positions(res)
                    for line in data:
                        cursor.execute("UPDATE position SET altitude=%s WHERE latitude=%s and longitude=%s",
                                     (line["elevation"], line["location"]["lat"], line["location"]["lng"]))
                    conn.commit()
                    if nb_res == 100:
                        sleep(1)  # API is limited to 1 call by sec
                else:
                    break
        except (ValueError, KeyError, requests.exceptions.RequestException):
            logger.error("Can't get altitude from API")

    @staticmethod
    def get_recorded_position():
        conn = Database.get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute('SELECT * FROM position ORDER BY Timestamp')
        res = cursor.fetchall()
        features_list = []
        for row in res:
            if row["longitude"] is None or row["latitude"] is None:
                continue
            feature = Feature(geometry=Point((row["longitude"], row["latitude"])),
                              properties={"vin": row["vin"], "date": row["Timestamp"].strftime("%x %X"),
                                          "mileage": row["mileage"],
                                          "level": row["level"], "level_fuel": row["level_fuel"]})
            features_list.append(feature)
        feature_collection = FeatureCollection(features_list)
        conn.close()
        return geo_dumps(feature_collection, sort_keys=True)

    @staticmethod
    def record_position(weather_api, vin, mileage, latitude, longitude, altitude, date, level, level_fuel, moving):
        if mileage == 0:  # fix a bug of the api
            logger.error("The api return a wrong mileage for %s : %f", vin, mileage)
        else:
            conn = Database.get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT Timestamp from position where Timestamp=%s", (date,))
            if cursor.fetchone() is None:
                temp = get_temp(latitude, longitude, weather_api)
                if level_fuel and level_fuel == 0:  # fix fuel level not provided when car is off
                    try:
                        cursor.execute(
                            "SELECT level_fuel FROM position WHERE level_fuel>0 AND VIN=%s ORDER BY Timestamp DESC "
                            "LIMIT 1",
                            (vin,))
                        level_fuel = cursor.fetchone()[0]
                        logger.info("level_fuel fixed with last real value %f for %s", level_fuel, vin)
                    except TypeError:
                        level_fuel = None
                        logger.info("level_fuel unfixed for %s", vin)

                cursor.execute("INSERT INTO position(Timestamp,VIN,longitude,latitude,altitude,mileage,level,level_fuel,"
                             "moving,temperature) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                             (date, vin, longitude, latitude, altitude, mileage, level, level_fuel, moving, temp))

                conn.commit()
                logger.info("new position recorded for %s", vin)
                Database.clean_position(conn)
                conn.close()
                return True
            conn.close()
            logger.debug("position already saved")
        return False

    @staticmethod
    def record_battery_soh(vin: str, date: datetime, level: float):
        conn = Database.get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO battery_soh(date, VIN, level) VALUES(%s,%s,%s)", (date, vin, level))
        conn.commit()
        conn.close()

    @staticmethod
    def get_soh_by_vin(vin) -> BatterySoh:
        conn = Database.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT date, level FROM battery_soh WHERE  VIN=%s ORDER BY date", (vin,))
        res = cursor.fetchall()
        dates = []
        levels = []
        for row in res:
            dates.append(row[0])
            levels.append(row[1])
        return BatterySoh(vin, dates, levels)

    @staticmethod
    def get_last_soh_by_vin(vin) -> float:
        conn = Database.get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT level FROM battery_soh WHERE  VIN=%s ORDER BY date DESC LIMIT 1", (vin,))
        res = cursor.fetchall()
        if res:
            return res[0][0]
        return None

    @staticmethod
    def get_last_charge(vin) -> Charge:
        conn = Database.get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM battery WHERE VIN=%s ORDER BY start_at DESC limit 1", (vin,))
        res = cursor.fetchone()
        if res:
            return Charge(**dict_key_to_lower_case(**res))
        return None

    @staticmethod
    def get_charge(vin, start_at) -> Charge:
        conn = Database.get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM battery WHERE VIN=%s AND START_AT=%s ORDER BY start_at DESC limit 1",
                           (vin, start_at,))
        res = cursor.fetchone()
        if res:
            return Charge(**dict_key_to_lower_case(**res))
        return None

    @staticmethod
    def get_all_charge_without_price(conn) -> List[Charge]:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM battery WHERE price IS NULL")
        res = cursor.fetchall()
        charges = []
        for row in res:
            charges.append(Charge(**dict_key_to_lower_case(**row)))
        return charges

    @staticmethod
    def get_all_charge() -> List[Charge]:
        conn = Database.get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("select * from battery ORDER BY start_at")
        res = cursor.fetchall()
        conn.close()
        return res

    @staticmethod
    def update_charge(charge: Charge):
        conn = Database.get_db()
        cursor = conn.cursor()
        res = cursor.execute(
            "UPDATE battery set stop_at=%s, end_level=%s, co2=%s, kw=%s, price=%s WHERE start_at=%s and VIN=%s",
            (charge.stop_at, charge.end_level, charge.co2, charge.kw, charge.price, charge.start_at,
             charge.vin))
        if cursor.rowcount == 0:
            logger.error("Can't find battery row to update")
        conn.commit()
        conn.close()
