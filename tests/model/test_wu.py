from contextlib import contextmanager
from datetime import datetime
from itertools import permutations
from operator import attrgetter
from unittest import TestCase, expectedFailure
from unittest.mock import MagicMock, patch

from flask_login import AnonymousUserMixin

from sipa.model.wu.user import User, UserDB
from sipa.model.wu.database_utils import STATUS
from sipa.model.wu.ldap_utils import UserNotFound, PasswordInvalid
from sipa.model.wu.schema import db, Nutzer, Buchung
from sipa.model.wu.factories import (ActiveNutzerFactory, InactiveNutzerFactory,
                                     UnknownStatusNutzerFactory,
                                     ComputerFactory, NutzerFactory,
                                     NoHostAliasComputerFactory,
                                     CreditFactory, TrafficFactory)
from sipa.utils import timetag_today
from tests.base import WuFrontendTestBase


class UserNoDBTestCase(TestCase):
    userdb_mock = MagicMock()

    def setUp(self):
        self.userdb_mock.reset_mock()

    def assert_userdata_passed(self, user, user_dict):
        self.assertEqual(user.realname, user_dict['name'])
        self.assertEqual(user.group, user_dict.get('group', 'passive'))
        self.assertEqual(user.mail, user_dict['mail'])

    @staticmethod
    def patch_user_group(user_dict):
        return patch(
            'sipa.model.wu.user.User.define_group',
            MagicMock(return_value=user_dict.get('group', 'passive'))
        )

    @patch('sipa.model.wu.user.UserDB', userdb_mock)
    def test_explicit_init(self):
        sample_user = {
            'uid': 'testnutzer',
            'name': "Test Nutzer",
            'mail': "test@nutzer.de",
            'group': 'passive',
        }

        with self.patch_user_group(sample_user):
            user = User(
                uid=sample_user['uid'],
                realname=sample_user['name'],
                mail=sample_user['mail'],
            )

        self.assert_userdata_passed(user, sample_user)
        assert self.userdb_mock.called

    @patch('sipa.model.wu.user.UserDB', userdb_mock)
    def test_define_group(self):
        sample_users = {
            # <uid>: <resulting_group>
            'uid1': 'passive',
            'uid2': 'active',
            'uid3': 'exactive',
        }

        def fake_search_in_group(uid, group_string):
            if group_string == "Aktiv":
                return sample_users[uid] == 'active'
            elif group_string == "Exaktiv":
                return sample_users[uid] == 'exactive'
            else:
                raise NotImplementedError

        for uid, group in sample_users.items():
            with patch('sipa.model.wu.user.search_in_group',
                       fake_search_in_group):
                user = User(uid=uid, realname="", mail="")
                with self.subTest(user=user):
                    self.assertEqual(user.define_group(), group)
        return

    @patch('sipa.model.wu.user.UserDB', userdb_mock)
    def test_get_constructor(self):
        test_users = [
            {'uid': "uid1", 'name': "Name Eins", 'mail': "test@foo.bar"},
            {'uid': "uid2", 'name': "Mareike Musterfrau", 'mail': "test@foo.baz"},
            {'uid': "uid3", 'name': "Deine Mutter", 'mail': "shizzle@damn.onion"},
        ]

        for test_user in test_users:
            with self.subTest(user=test_user):

                with self.patch_user_group(test_user), \
                     patch('sipa.model.wu.user.LdapConnector') as LdapConnectorMock:
                        LdapConnectorMock.fetch_user.return_value = test_user

                        user = User.get(test_user['uid'])
                        assert LdapConnectorMock.fetch_user.called

                self.assertIsInstance(user, User)
                self.assert_userdata_passed(user, test_user)

    def test_get_constructor_returns_anonymous(self):
        with patch('sipa.model.wu.user.LdapConnector') as LdapConnectorMock:
            LdapConnectorMock.fetch_user.return_value = None
            user = User.authenticate("foo", "bar")
        self.assertIsInstance(user, AnonymousUserMixin)

    def test_authentication_passing(self):
        """Test correct instanciation behaviour of `User.authenticate`.

        It is checked whether the ldap is called correctly and
        instanciation is done using `User.get`.
        """
        sample_users = [
            ("uid", "pass"),
            ("foo", "bar"),
            ("baz", None),
        ]
        for uid, password in sample_users:
            with patch('sipa.model.wu.user.LdapConnector') as ldap_mock, \
                 patch('sipa.model.wu.user.User.get') as get_mock, \
                 self.subTest(uid=uid, password=password):

                User.authenticate(uid, password)

                self.assertEqual(ldap_mock.call_args[0], (uid, password))
                self.assertEqual(get_mock.call_args[0], (uid,))

    def test_authentication_reraise_unknown_user(self):
        """Test that certain exceptions are re-raised by `User.authenticate`.

        Objects of interest are `UserNotFound`, `PasswordInvalid`.
        """
        for exception_class in [UserNotFound, PasswordInvalid]:
            def raise_exception(): raise exception_class()
            with patch('sipa.model.wu.user.LdapConnector') as ldap_mock, \
                    self.subTest(exception_class=exception_class):

                ldap_mock().__enter__.side_effect = raise_exception
                with self.assertRaises(exception_class):
                    User.authenticate(username=None, password=None)


class WuAtlantisFakeDBInitialized(WuFrontendTestBase):
    def create_app(self, *a, **kw):
        config = {
            **kw.pop('additional_config', {}),
            'DB_NETUSERS_URI': "sqlite:///",
            'DB_TRAFFIC_URI': "sqlite:///",
            'DB_USERMAN_URI': "postgresql://sipa:password@postgres:5432/userman",  # noqa
            'DB_HELIOS_IP_MASK': "10.10.7.%",
        }
        return super().create_app(*a, **kw, additional_config=config)

    def setUp(self):
        db.drop_all()
        db.create_all()

    def assert_computer_data_passed(self, computer, user):
        self.assertIn(computer.c_ip, user.ips)
        self.assertIn(computer.c_etheraddr.upper(), user.mac)
        self.assertIn(computer.c_hname, user.hostname)
        self.assertIn(computer.c_alias, user.hostalias)

    @staticmethod
    def create_user_ldap_patched(uid, name, mail):
        with patch('sipa.model.wu.user.search_in_group',
                   MagicMock(return_value=False)):
            return User(
                uid=uid,
                realname=name,
                mail=mail,
            )


class UserWithDBTestCase(WuAtlantisFakeDBInitialized):
    def setUp(self):
        super().setUp()

        self.nutzer = NutzerFactory.create(status=1)
        self.computer = ComputerFactory.create(nutzer=self.nutzer)

    @staticmethod
    def get_sql_user_from_login(unix_account):
        return db.session.query(Nutzer).filter_by(unix_account=unix_account).one()

    def test_from_ip_returns_anonymous(self):
        user = User.from_ip("141.30.228.66")
        self.assertIsInstance(user, AnonymousUserMixin)

    def test_from_ip_user_returned(self):
        with patch('sipa.model.wu.user.User.get') as user_get_mock:
            user_get_mock.side_effect = self.get_sql_user_from_login

            user = User.from_ip(self.computer.c_ip)
            self.assertEqual(user_get_mock.call_args[0],
                             (self.nutzer.unix_account,))
            self.assertEqual(user, self.nutzer)

    def test_from_ip_user_not_in_ldap(self):
        with patch('sipa.model.wu.user.User.get', MagicMock(return_value=None)), \
                patch('sipa.model.wu.user.logger.warning') as warning_mock:
            user = User.from_ip(self.computer.c_ip)
            self.assertIsInstance(user, AnonymousUserMixin)
            self.assertIn(" could not ", warning_mock.call_args[0][0])
            self.assertIn("LDAP", warning_mock.call_args[0][0])


class UserNoComputersTestCase(WuAtlantisFakeDBInitialized):
    def setUp(self):
        super().setUp()
        self.nutzer = NutzerFactory.create()
        self.user = self.create_user_ldap_patched(
            uid=self.nutzer.unix_account,
            name=None,
            mail=None,
        )

    def test_user_has_no_computer_data(self):
        self.assertFalse(self.user.mac)
        self.assertFalse(self.user.ips)
        self.assertFalse(self.user.hostname)
        self.assertFalse(self.user.hostalias)


class TestUserInitializedCase(WuAtlantisFakeDBInitialized):
    def setUp(self):
        super().setUp()

        self.nutzer = NutzerFactory.create(status=1)
        self.computers = ComputerFactory.create_batch(20, nutzer=self.nutzer)

        self.name = "Test Nutzer"
        self.mail = "foo@bar.baz"

        self.user = self.create_user_ldap_patched(
            uid=self.nutzer.unix_account,
            name=self.name,
            mail=self.mail,
        )

    def test_mail_passed(self):
        self.assertEqual(self.user.mail, self.mail)

    def test_computer_data_passed(self):
        for computer in self.computers:
            with self.subTest(computer=computer):
                self.assert_computer_data_passed(computer, self.user)

    def test_address_passed(self):
        self.assertEqual(self.user.address, self.nutzer.address)

    def test_id_passed(self):
        original_id = str(self.nutzer.nutzer_id)
        fetched_id = self.user.id.value
        self.assertTrue(
            fetched_id.startswith(original_id),
            msg="id '{}' doesn't start with '{}'".format(fetched_id,
                                                         original_id)
        )


class ComputerWithoutAliasTestCase(WuAtlantisFakeDBInitialized):
    def setUp(self):
        super().setUp()

        self.nutzer = NutzerFactory.create(status=1)

        self.name = "Test Nutzer"
        self.mail = "foo@bar.baz"

        self.user = self.create_user_ldap_patched(
            uid=self.nutzer.unix_account,
            name=self.name,
            mail=self.mail,
        )

    @contextmanager
    def fail_on_type_error(self):
        try:
            yield
        except TypeError:
            self.fail("Accessing user.hostalias with one alias being None "
                      "threw a TypeError")

    def test_computer_no_alias_returns_empty_string(self):
        NoHostAliasComputerFactory.create(nutzer=self.nutzer)
        with self.fail_on_type_error():
            self.assertEqual(self.user.hostalias, "")

    def test_computer_no_alias_not_included(self):
        self.computers = ComputerFactory.create_batch(2, nutzer=self.nutzer)
        self.computers += NoHostAliasComputerFactory.create_batch(2, nutzer=self.nutzer)
        with self.fail_on_type_error():
            for computer in self.computers:
                if not computer.c_alias:
                    continue

                with self.subTest(computer=computer):
                    self.assertIn(computer.c_alias, self.user.hostalias)


class UserStatusGivenCorrectly(WuAtlantisFakeDBInitialized):
    def setUp(self):
        super().setUp()
        self.valid_status_nutzer_list = NutzerFactory.create_batch(20)
        self.unknown_status_nutzer_list = UnknownStatusNutzerFactory.create_batch(20)

    def test_unknown_status_empty(self):
        for nutzer in self.unknown_status_nutzer_list:
            user = self.create_user_ldap_patched(
                uid=nutzer.unix_account,
                name=None,
                mail=None,
            )
            with self.subTest(user=user):
                self.assertFalse(user.status)

    def test_known_status_passed(self):
        for nutzer in self.valid_status_nutzer_list:
            user = self.create_user_ldap_patched(
                uid=nutzer.unix_account,
                name=None,
                mail=None,
            )
            with self.subTest(user=user):
                self.assertEqual(STATUS[nutzer.status][0], user.status)


class CorrectUserHasConnection(WuAtlantisFakeDBInitialized):
    def setUp(self):
        super().setUp()
        self.connection_nutzer_list = ActiveNutzerFactory.create_batch(20)
        self.no_connection_nutzer_list = InactiveNutzerFactory.create_batch(20)

    def test_correct_users_have_connection(self):
        for nutzer in self.connection_nutzer_list:
            user = self.create_user_ldap_patched(
                uid=nutzer.unix_account,
                name=None,
                mail=None
            )
            with self.subTest(user=user):
                self.assertTrue(user.has_connection)

    def test_incorrect_users_no_connection(self):
        for nutzer in self.no_connection_nutzer_list:
            user = self.create_user_ldap_patched(
                uid=nutzer.unix_account,
                name=None,
                mail=None,
            )
            with self.subTest(user=user):
                self.assertFalse(user.has_connection)


class OneUserWithCredit(WuAtlantisFakeDBInitialized):
    def setUp(self):
        super().setUp()
        self.nutzer = NutzerFactory.create()
        self.credit_entries = []
        self.timetag_range = range(timetag_today()-21, timetag_today())
        for timetag in self.timetag_range:
            self.credit_entries.append(CreditFactory.create(nutzer=self.nutzer,
                                                            timetag=timetag))


class CreditTestCase(OneUserWithCredit):
    def setUp(self):
        super().setUp()

    def test_credit_passed(self):
        fetched_user = self.create_user_ldap_patched(
            uid=self.nutzer.unix_account,
            name=None,
            mail=None,
        )
        expected_credit = self.credit_entries[-1].amount
        self.assertEqual(fetched_user.credit, expected_credit)

    def test_credit_appears_in_history(self):
        fetched_history = self.create_user_ldap_patched(
            uid=self.nutzer.unix_account,
            name=None,
            mail=None,
        ).traffic_history
        # the history is ascending, but wee ned a zip ending at
        # *today* (last element)
        combined_history = zip(reversed(self.credit_entries), reversed(fetched_history))

        for expected_credit, traffic_entry in combined_history:
            with self.subTest(traffic_entry=traffic_entry):
                self.assertEqual(traffic_entry['credit'], expected_credit.amount)


class TrafficOneComputerTestCase(OneUserWithCredit):
    def setUp(self):
        super().setUp()
        self.computer = ComputerFactory(nutzer=self.nutzer)
        self.traffic_entries = []
        for timetag in range(timetag_today()-21, timetag_today()):
            traffic_entry = TrafficFactory.create(timetag=timetag, ip=self.computer.c_ip)
            self.traffic_entries.append(traffic_entry)

    def test_traffic_data_passed(self):
        fetched_history = self.create_user_ldap_patched(
            uid=self.nutzer.unix_account,
            name=None,
            mail=None,
        ).traffic_history
        # the history is ascending, but wee ned a zip ending at
        # *today* (last element)
        combined_history = zip(reversed(self.traffic_entries), reversed(fetched_history))

        for expected_traffic, traffic_entry in combined_history:
            with self.subTest(traffic_entry=traffic_entry):
                self.assertEqual(traffic_entry['input'], traffic_entry['input'])
                self.assertEqual(traffic_entry['output'], traffic_entry['output'])


class IPMaskValidityChecker(TestCase):
    """Tests concerning the validation of ip masks passed to the
    `UserDB`
    """

    def setUp(self):
        valid_elements = ['1', '125', '255', '%']
        self.valid = list(permutations(valid_elements, 4))

        # probably not the most elegant choices, but that should do the trick
        invalid_elements = ['%%', '%%%', '1%1', '1%%1']
        self.invalid = []
        for p in self.valid:
            p = list(p)
            for inv in invalid_elements:
                self.invalid += [p[:i] + [inv] + p[i+1:] for i in range(4)]

    def test_invalid_ips_raise(self):
        """Test that passing invalid ip masks raises a `ValueError`"""
        for ip_tuple in self.invalid:
            with self.subTest(ip_tuple=ip_tuple):
                with self.assertRaises(ValueError):
                    UserDB.test_ipmask_validity(".".join(ip_tuple))

    def test_valid_ips_pass(self):
        """Test that passing valid ip masks works"""
        for ip_tuple in self.valid:
            with self.subTest(ip_tuple=ip_tuple):
                try:
                    UserDB.test_ipmask_validity(".".join(ip_tuple))
                except ValueError:
                    self.fail("`test_ipmask_validity` raised ValueError "
                              "on correct ip '{}'".format(".".join(ip_tuple)))


class UsermanInitializedTestCase(WuAtlantisFakeDBInitialized):
    def test_userman_in_binds(self):
        self.assertIn('userman', self.app.config['SQLALCHEMY_BINDS'].keys())

    def test_userman_correctly_initialized(self):
        self.assertFalse(db.session.query(Buchung).all())


class FinanceBalanceTestCase(OneUserWithCredit):
    def setUp(self):
        super().setUp()
        self.transactions = [
            Buchung(wert=-350, soll_uid=self.nutzer.nutzer_id, haben_uid=None,
                    bes="Freischalten!!!", datum=datetime(2016, 6, 1)),
            Buchung(wert=350, soll_uid=self.nutzer.nutzer_id, haben_uid=None,
                    bes="Semesterbeitrag 04/16", datum=datetime(2016, 4, 30)),
            Buchung(wert=350, soll_uid=self.nutzer.nutzer_id, haben_uid=None,
                    bes="Semesterbeitrag 05/16", datum=datetime(2016, 5, 30)),
        ]
        for t in self.transactions:
            db.session.add(t)
        db.session.commit()
        self.user = self.create_user_ldap_patched(
            uid=self.nutzer.unix_account,
            name=None,
            mail=None,
        )

    def test_correct_number_of_transactions(self):
        recvd_transactions = db.session.query(Nutzer).one().transactions
        self.assertEqual(len(self.transactions), len(recvd_transactions))

    def test_user_has_correct_balance(self):
        expected_balance = 3.5
        self.assertEqual(self.user.finance_balance, expected_balance)

    def test_finance_date_max_in_database(self):
        expected_date = max(t.datum for t in self.transactions)
        self.assertEqual(self.user.last_finance_update, expected_date)

    def test_finance_logs_is_duple(self):
        for log in self.user.finance_logs:
            with self.subTest(log=log):
                self.assertEqual(len(log), 2)

    def test_user_has_correct_logs(self):
        expected_logs = sorted([t.unsafe_as_tuple() for t in self.transactions],
                               key=attrgetter('datum'))
        self.assertEqual(self.user.finance_logs, expected_logs)

    def test_finance_logs_sorted_by_date(self):
        logs = self.user.finance_logs
        last_log = None
        for log in logs:
            if last_log is not None:
                self.assertLessEqual(last_log[0], log[0])
            last_log = log


class HabenSollSwitchedTestCase(OneUserWithCredit):
    def setUp(self):
        super().setUp()
        self.transactions = [
            Buchung(wert=-350, soll_uid=self.nutzer.nutzer_id, haben_uid=None,
                    bes="Freischalten!!!", datum=datetime(2016, 6, 1)),
            Buchung(wert=350, soll_uid=self.nutzer.nutzer_id, haben_uid=None,
                    bes="Semesterbeitrag 04/16", datum=datetime(2016, 4, 30)),
            Buchung(wert=-350, haben_uid=self.nutzer.nutzer_id, soll_uid=None,
                    bes="Semesterbeitrag 05/16", datum=datetime(2016, 5, 30)),
        ]
        for t in self.transactions:
            db.session.add(t)
        db.session.commit()
        self.user = self.create_user_ldap_patched(
            uid=self.nutzer.unix_account,
            name=None,
            mail=None,
        )

    def test_user_has_correct_balance(self):
        expected_balance = 3.5
        self.assertEqual(self.user.finance_balance, expected_balance)
