# -*- coding: utf-8 -*-
from contextlib import contextmanager

from flask.ext.babel import gettext
from flask.ext.login import AnonymousUserMixin
from sqlalchemy.exc import OperationalError

from model.property import active_prop

from model.default import BaseUser, BaseUserDB
from model.wu.database_utils import sql_query, \
    update_macaddress, query_trafficdata, \
    create_mysql_userdatabase, drop_mysql_userdatabase, \
    change_mysql_userdatabase_password, user_has_mysql_db, \
    calculate_userid_checksum, DORMITORIES, STATUS, \
    timetag_from_timestamp
from model.wu.ldap_utils import search_in_group, LdapConnector, \
    change_email, change_password

from sipa.utils import argstr
from sipa.utils.exceptions import PasswordInvalid, UserNotFound, DBQueryEmpty

import logging
logger = logging.getLogger(__name__)


class User(BaseUser):
    """User object will be created from LDAP credentials,
    only stored in session.

    the terms 'uid' and 'username' refer to the same thing.
    """

    datasource = 'wu'

    def __init__(self, uid, name, mail):
        super(User, self).__init__(uid)
        self.name = name
        self.group = self.define_group()
        self._mail = mail
        self.cache_information()
        self._userdb = UserDB(self)

    def __repr__(self):
        return "{}.{}({})".format(__name__, type(self).__name__, argstr(
            uid=self.uid,
            name=self.name,
            mail=self._mail,
        ))

    def __str__(self):
        return "User {} ({}), {}".format(self.name, self.uid, self.group)

    can_change_password = True

    def define_group(self):
        """Define a user group from the LDAP group
        """
        if search_in_group(self.uid, 'Aktiv'):
            return 'active'
        elif search_in_group(self.uid, 'Exaktiv'):
            return 'exactive'
        return 'passive'

    @classmethod
    def get(cls, username, **kwargs):
        """Static method for flask-login user_loader,
        used before _every_ request.
        """
        user = LdapConnector.fetch_user(username)
        if user:
            return cls(user['uid'], user['name'], user['mail'], **kwargs)
        return AnonymousUserMixin()

    @classmethod
    def authenticate(cls, username, password):
        """This method checks the user and password combination against LDAP

        Returns the User object if successful.
        """
        try:
            with LdapConnector(username, password):
                return cls.get(username)
        except PasswordInvalid:
            logger.info('Failed login attempt (Wrong %s)', 'password',
                        extra={'data': {'username': username}})
            raise
        except UserNotFound:
            logger.info('Failed login attempt (Wrong %s)', 'username',
                        extra={'data': {'username': username}})
            raise

    @classmethod
    def from_ip(cls, ip):
        result = sql_query("SELECT c.nutzer_id FROM computer as c "
                           "LEFT JOIN nutzer as n "
                           "ON c.nutzer_id = n.nutzer_id "
                           "WHERE c_ip = %s "
                           "AND (n.status < 8 OR n.status > 10) "
                           "ORDER BY c.nutzer_id DESC",
                           (ip,)).fetchone()
        if result is None:
            return AnonymousUserMixin()

        username = sql_query("SELECT unix_account FROM nutzer "
                             "WHERE nutzer_id = %s",
                             (result['nutzer_id'],)).fetchone()['unix_account']

        user = cls.get(username)
        if not user:
            logger.warning("User %s could not be fetched from LDAP",
                           username, extra={'data': {
                               'username': username,
                               'user_id': result['nutzer_id'],
                           }})
            return AnonymousUserMixin()

        return user

    def change_password(self, old, new):
        """Change a user's password from old to new
        """
        try:
            change_password(self.uid, old, new)
        except PasswordInvalid:
            logger.info('Wrong password provided when attempting '
                        'change of password')
            raise
        else:
            logger.info('Password successfully changed')

    def cache_information(self):
        user = sql_query(
            "SELECT nutzer_id, wheim_id, etage, zimmernr, status "
            "FROM nutzer "
            "WHERE unix_account = %s",
            (self.uid,)
        ).fetchone()

        if not user:
            # TODO: more information on this very specific issue.
            raise DBQueryEmpty

        self._id = user['nutzer_id']
        self._address = "{0} / {1} {2}".format(
            # MySQL Dormitory IDs in are from 1-11, so we map to 0-10 with x-1
            DORMITORIES[user['wheim_id'] - 1],
            user['etage'],
            user['zimmernr']
        )
        self._status_id = user['status']

        devices = sql_query(
            "SELECT c_etheraddr, c_ip, c_hname, c_alias "
            "FROM computer "
            "WHERE nutzer_id = %s",
            (user['nutzer_id'])
        ).fetchall()

        if devices:
            self._devices = [{
                'ip': device['c_ip'],
                'mac': device['c_etheraddr'].upper(),
                'hostname': device['c_hname'],
                'hostalias': device['c_alias'],
            } for device in devices]
        else:
            logger.warning("User {} (id {}) does not have any devices"
                           .format(self.uid, self._id))
            self._devices = []

        # cache credit
        current_timetag = timetag_from_timestamp()

        try:
            # aggregated credit from 1(MEZ)/2(MESZ) AM
            credit = sql_query(
                "SELECT amount FROM credit "
                "WHERE user_id = %(id)s "
                "AND timetag = %(today)s ",
                {'today': current_timetag, 'id': self. _id}
            ).fetchone()['amount']

            # subtract the current traffic not yet aggregated in `credit`
            traffic = sql_query(
                "SELECT input + output as throughput "
                "FROM traffic.tuext AS t "
                "LEFT JOIN computer AS c on c.c_ip = t.ip "
                "WHERE c.nutzer_id =  %(id)s AND t.timetag = %(today)s",
                {'today': current_timetag, 'id': self._id}
            ).fetchone()

            credit -= traffic['throughput']

        except OperationalError as e:
            logger.critical("Unable to connect to MySQL server",
                            extra={'data': {'exception_args': e.args}})
            self._credit = None
            raise

        else:
            self._credit = round(credit / 1024, 2)

    @property
    def traffic_history(self):
        # TODO: which IP to use?
        # TODO: this throws DBQueryEmpty
        return query_trafficdata(self._devices[0]['ip'], self._id)

    @property
    def credit(self):
        """Return the current credit that is left

        Note that the data doesn't have to be cached again, because
        `__init__` is called before every request.
        """
        return self._credit

    @contextmanager
    def tmp_authentication(self, password):
        """Check and temporarily store the given password.

        Returns a context manager.  The password is stored in
        `self.__password`.

        This is quite an ugly hack, only existing because sipa does
        not have an ldap bind for this datasource and needs the user's
        password.  THe need for the password breaks compatability with
        the usual `instance.property = value` – now, an AttributeError
        has to be catched and in that case this wrapper has to be used.

        I could not think of a better way to get around this.

        """
        self.re_authenticate(password)
        self.__password = password
        yield
        del self.__password

    @active_prop
    def login(self):
        return self.uid

    @active_prop
    def mac(self):
        return {'value': ", ".join(device['mac'] for device in self._devices),
                'tmp_readonly': len(self._devices) > 1}

    @mac.setter
    def mac(self, new_mac):
        assert len(self._devices) == 1, ""
        update_macaddress(self._devices[0]['ip'], self.mac.value, new_mac)

    @active_prop
    def mail(self):
        return self._mail

    @mail.setter
    def mail(self, new_mail):
        change_email(self.uid, self.__password, new_mail)

    @mail.deleter
    def mail(self):
        self.mail = ''

    @active_prop
    def address(self):
        return self._address

    @active_prop
    def ips(self):
        return ", ".join(device['ip'] for device in self._devices)

    @active_prop
    def status(self):
        if self._status_id in STATUS:
            status_tuple = STATUS[self._status_id]
            return {'value': status_tuple[0], 'style': status_tuple[1]}

        return {'value': STATUS.get(self._status_id, gettext("Unbekannt")),
                'empty': True}

    @active_prop
    def id(self):
        return "{}-{}".format(
            self._id,
            calculate_userid_checksum(self._id),
        )

    @active_prop
    def hostname(self):
        return ", ".join(device['hostname'] for device in self._devices)

    @active_prop
    def hostalias(self):
        return ", ".join(device['hostalias'] for device in self._devices)

    @active_prop
    def userdb_status(self):
        try:
            status = self.userdb.has_db
        except OperationalError:
            return {'value': gettext("Datenbank nicht erreichbar"),
                    'style': 'danger', 'empty': True}

        if status:
            return {'value': gettext("Aktiviert"),
                    'style': 'success'}
        return {'value': gettext("Nicht aktiviert"),
                'empty': True}

    userdb_status = userdb_status.fake_setter()

    @property
    def userdb(self):
        return self._userdb


class UserDB(BaseUserDB):
    def __init__(self, user):
        super(UserDB, self).__init__(user)

    @property
    def has_db(self):
        try:
            if user_has_mysql_db(self.user.uid):
                return True
            return False
        except OperationalError:
            logger.critical("User db of %s unreachable", self.user)
            raise

    def create(self, password):
        create_mysql_userdatabase(self.user.uid, password)

    def drop(self):
        drop_mysql_userdatabase(self.user.uid)

    def change_password(self, password):
        change_mysql_userdatabase_password(self.user.uid, password)
