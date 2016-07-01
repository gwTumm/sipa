# -*- coding: utf-8; -*-
from sqlalchemy import (Column, Index, Integer, String,
                        text, Text, ForeignKey, DECIMAL, BigInteger, Date, case, or_)
from sqlalchemy.orm import relationship, column_property, object_session

from sipa.model.sqlalchemy import db
from sipa.model.misc import TransactionTuple

import logging
logger = logging.getLogger(__name__)


DORMITORY_MAPPINGS = [
    'Wundstraße 5',
    'Wundstraße 7',
    'Wundstraße 9',
    'Wundstraße 11',
    'Wundstraße 1',
    'Wundstraße 3',
    'Zellescher Weg 41',
    'Zellescher Weg 41A',
    'Zellescher Weg 41B',
    'Zellescher Weg 41C',
    'Zellescher Weg 41D',
    'Borsbergstraße 34',
    'Zeunerstraße 1f',
]


class Nutzer(db.Model):
    __tablename__ = 'nutzer'
    __table_args__ = (
        Index(u'zimmer', u'etage', u'zimmernr'),
    )

    nutzer_id = Column(Integer, primary_key=True, server_default=text("'0'"))
    wheim_id = Column(Integer, nullable=False, index=True,
                      server_default=text("'0'"))
    etage = Column(Integer, nullable=False, server_default=text("'0'"))
    zimmernr = Column(String(10), nullable=False, server_default=text("''"))
    unix_account = Column(String(40), nullable=False, unique=True)
    status = Column(Integer, nullable=False, index=True,
                    server_default=text("'1'"))

    computer = relationship("Computer", backref="nutzer")
    credit_entries = relationship('Credit', backref="nutzer")

    @property
    def address(self):
        try:
            return "{} / {} {}".format(
                DORMITORY_MAPPINGS[self.wheim_id - 1],
                self.etage,
                self.zimmernr,
            )
        except IndexError:
            logger.warning("No dormitory mapping given for `wheim_id`=%s",
                           self.wheim_id)
            return ""

    @property
    def transactions(self):
        """The transactions of the Nutzer.

        This performs a query against `Buchung`, correcting the value
        by negation depending on where in `haben_uid` or `soll_uid`
        the `nutzer_id` appears.

        :return: The triple `(date, value, description)` with the
        value being euros.
        """
        session = object_session(self)
        return [
            TransactionTuple(*result) for result in
            session.query(
                Buchung.datum,
                case([(Buchung.haben_uid.is_(None), Buchung.wert)],
                     else_=(-Buchung.wert)) / 100.0,
            ).filter(
                or_(Buchung.haben_uid == self.nutzer_id,
                    Buchung.soll_uid == self.nutzer_id)
            ).order_by(Buchung.datum.asc()).all()
        ]


class Computer(db.Model):
    __tablename__ = 'computer'

    nutzer_id = Column(Integer, ForeignKey('nutzer.nutzer_id'), nullable=False)

    c_etheraddr = Column(String(20), primary_key=True)
    c_ip = Column(String(15), nullable=False, index=True, primary_key=True,
                  server_default=text("''"))
    c_hname = Column(String(20), nullable=False, server_default=text("''"))
    c_alias = Column(String(20))


class Credit(db.Model):
    __tablename__ = u'credit'

    user_id = Column(Integer, ForeignKey('nutzer.nutzer_id'),
                     primary_key=True, nullable=False)
    amount = Column(Integer, nullable=False)
    timetag = Column(Integer, primary_key=True, nullable=False)


class Traffic(db.Model):
    __tablename__ = 'tuext'
    __bind_key__ = 'traffic'

    timetag = Column(BigInteger(), primary_key=True)
    ip = Column(String(15), nullable=False, index=True, primary_key=True)
    input = Column(DECIMAL(20, 0))
    output = Column(DECIMAL(20, 0))
    overall = column_property(input + output)


class Buchung(db.Model):
    __tablename__ = 'buchungen'
    __bind_key__ = 'userman'

    oid = Column(Integer, nullable=False, primary_key=True)
    bkid = Column(Integer)
    fbid = Column(Integer)

    datum = Column(Date, nullable=False, default='')
    wert = Column(Integer, nullable=False, default=0)
    bes = Column(Text)

    soll_uid = Column(Integer)
    haben_uid = Column(Integer)

    def __repr__(self):
        return (
            "<{cls} {wert:.2f}€ Soll: {s_uid} → Haben: {h_uid} '{bes}'>"
            .format(
                cls=type(self).__name__,
                wert=self.wert/100,
                s_uid=self.soll_uid,
                h_uid=self.haben_uid,
                bes=self.bes,
            )
        )

    def unsafe_as_tuple(self):
        """Return self as a TransactionTuple

        This might be useful for comparisons.  This code doesn't care
        about the effective value as corrected in the sql
        case-statement of `Nutzer`, so DO NOT USE IT except for tests!
        """
        return TransactionTuple(self.datum, self.wert / 100.0)
