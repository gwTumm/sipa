#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Drupalport auf Python im Zuge der Entwicklung von Pycroft.
Erstellt am 02.03.2014 von Dominik Pataky pataky@wh2.tu-dresden.de
"""

from flask import Flask, render_template, request, redirect, \
    url_for, flash, send_file, session
from flask.ext.login import LoginManager, current_user, login_user, \
    logout_user, login_required
from flask.ext.babel import Babel, gettext
import io
from sqlalchemy.exc import OperationalError
from ldap import SERVER_DOWN

from config import languages, busstops
from forms import flash_formerrors, ContactForm, ChangePasswordForm, \
    ChangeMailForm, LoginForm, ChangeMACForm
from utils import calculate_userid_checksum, get_bustimes
from utils.database_utils import query_userinfo, query_trafficdata, update_macaddress
from utils.graph_utils import make_trafficgraph
from utils.ldap_utils import User, authenticate, change_password, change_email
from utils.mail_utils import send_mail

app = Flask(__name__)
app.secret_key = "q_T_a1C18aizPnA2yf-1Q8(2&,pd5n"
login_manager = LoginManager()
login_manager.init_app(app)

babel = Babel(app)


def errorpage(e):
    if e.code in (404,):
        flash(gettext(u"Seite nicht gefunden!"), "warning")
    elif e.code in (401, 403):
        flash(gettext(u"Sie haben nicht die notwendigen Rechte um die Seite zu sehen!"), "warning")
    else:
        flash(gettext(u"Es ist ein Fehler aufgetreten!"), "error")
    return redirect(url_for("index"))
app.register_error_handler(401, errorpage)
app.register_error_handler(403, errorpage)
app.register_error_handler(404, errorpage)


@app.errorhandler(OperationalError)
def exceptionhandler_sql(ex):
    """Handles global MySQL errors (server down).
    """
    flash(u"Connection to SQL server could not be established!", "error")
    return redirect(url_for('index'))


@app.errorhandler(SERVER_DOWN)
def exceptionhandler_ldap(ex):
    """Handles global LDAP SERVER_DOWN exceptions.
    The session must be reset, because if the user is logged in and the server
    fails during his session, it would cause a redirect loop.
    This also resets the language choice, btw.

    The alternative would be a try-except catch block in load_user, but login
    also needs a handler.
    """
    session.clear()
    flash(u"Connection to LDAP server could not be established!", "error")
    return redirect(url_for('index'))


@login_manager.user_loader
def load_user(username):
    """Loads a User object from/into the session at every request
    """
    return User.get(username)


@babel.localeselector
def babel_selector():
    """Tries to get the language setting from the current session cookie.
    If this fails (if it is not set) the best matching language out of the
    header accept-language is chosen and set.
    """
    lang = session.get('lang')
    if not lang:
        session['lang'] = request.accept_languages.best_match(languages.keys())

    return session.get('lang')


@app.route('/')
def index():
    return render_template("index.html")


@app.route("/contacts")
def contacts():
    return render_template("content/ansprechpartner.html")


@app.route("/login", methods=['GET', 'POST'])
def login():
    """Login page for users
    """
    form = LoginForm()

    if form.validate_on_submit():
        username = form.username.data
        password = form.password.data
        user = authenticate(username, password)

        if user == -1:
            flash(gettext(u"Nutzer nicht gefunden!"), "error")
        elif user == -2:
            flash(gettext(u"Passwort war inkorrekt!"), "error")

        if isinstance(user, User):
            login_user(user)
    elif form.is_submitted():
        flash_formerrors(form)

    if current_user.is_authenticated():
        return redirect(url_for('usersuite'))

    return render_template('login.html', form=form)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/language/<string:lang>")
def set_language(lang='de'):
    """Set the session language via URL
    """
    session['lang'] = lang
    return redirect(request.referrer)


@app.route("/usersuite")
@login_required
def usersuite():
    """Usersuite landing page with user account information
    and traffic overview.
    """
    userinfo = query_userinfo(current_user.uid)

    if userinfo == -1:
        flash(gettext(u"Es gab einen Fehler bei der Datenbankanfrage!"), "error")
        return redirect(url_for("index"))

    userinfo['checksum'] = calculate_userid_checksum(userinfo['id'])
    trafficdata = query_trafficdata(userinfo['ip'])

    return render_template("usersuite/index.html",
                           userinfo=userinfo,
                           usertraffic=trafficdata)


@app.route("/usersuite/contact", methods=['GET', 'POST'])
@login_required
def usersuite_contact():
    """Contact form for logged in users.
    Currently sends an e-mail to the support mailing list as
    '[Usersuite] Category: Subject' with userid and message.
    """
    form = ContactForm()

    if form.validate_on_submit():
        types = {
            'stoerung': u"Störung",
            'finanzen': u"Finanzen",
            'eigene-technik': u"Eigene Technik"
        }

        cat = form.type.data
        if cat in types.keys():
            cat = types[cat]
        else:
            cat = u"Allgemein"

        subject = u"[Usersuite] {0}: {1}".format(cat, form.subject.data)

        message_text = u"Nutzerlogin: {0}\n\n".format(current_user.uid) \
                       + form.message.data

        if send_mail(form.email.data, "support@wh2.tu-dresden.de", subject, message_text):
            flash(gettext(u"Nachricht wurde versandt."), "success")
        else:
            flash(gettext(u"Es gab einen Fehler beim Versenden der Nachricht. Bitte schicke uns direkt eine E-Mail an support@wh2.tu-dresden.de"), "error")
        return redirect(url_for("usersuite"))
    elif form.is_submitted():
        flash_formerrors(form)

    return render_template("usersuite/contact.html", form=form)


@app.route("/usersuite/change-password", methods=['GET', 'POST'])
@login_required
def usersuite_change_password():
    """Lets the user change his password.
    Requests the old password once (in case someone forgot to logout for
    example) and the new password two times.

    If the new password was entered correctly twice, LDAP performs a bind
    with the old credentials at the users DN and submits the passwords to
    ldap.passwd_s(). This way every user can edit only his own data.

    Error code "-1" is an incorrect old or empty password.

    TODO: set a minimum character limit for new passwords.
    """
    form = ChangePasswordForm()

    if form.validate_on_submit():
        old = form.old.data
        new = form.new.data

        if new != form.new2.data:
            flash(gettext(u"Neue Passwörter stimmen nicht überein!"), "error")
        else:
            code = change_password(current_user.uid, old, new)
            if code == -2:
                flash(gettext(u"Altes Passwort war inkorrekt!"), "error")
            elif code:
                flash(gettext(u"Passwort wurde geändert"), "success")
                return redirect(url_for("usersuite"))
    elif form.is_submitted():
        flash_formerrors(form)

    return render_template("usersuite/change_password.html", form=form)


@app.route("/usersuite/change-mail", methods=['GET', 'POST'])
@login_required
def usersuite_change_mail():
    """Changes the users forwarding mail attribute
    in his LDAP entry.

    TODO: LDAP schema forbids add/replace 'mail' attribute
    """
    form = ChangeMailForm()

    if form.validate_on_submit():
        password = form.password.data
        email = form.email.data


        code = change_email(current_user.uid, password, email)
        if code == -1:
            flash(gettext(u"Nutzer nicht gefunden!"), "error")
        elif code == -2:
            flash(gettext(u"Passwort war inkorrekt!"), "error")
        elif code == -3:
            flash(gettext(u"Nicht genügend LDAP-Rechte!"), "error")
        else:
            flash(gettext(u"E-Mail-Adresse wurde geändert"), "success")
            return redirect(url_for('usersuite'))
    elif form.is_submitted():
        flash_formerrors(form)

    return render_template('usersuite/change_mail.html', form=form)


@app.route("/usersuite/change-mac", methods=['GET', 'POST'])
@login_required
def usersuite_change_mac():
    """As user, change the MAC address of your device.
    """
    form = ChangeMACForm()
    userinfo = query_userinfo(current_user.uid)

    if form.validate_on_submit():
        password = form.password.data
        mac = form.mac.data

        auth = authenticate(current_user.uid, password)
        if not isinstance(auth, int):
            update_macaddress(userinfo['ip'], userinfo['mac'], mac)

            subject = u"[Usersuite] %s hat seine/ihre MAC-Adresse " \
                      u"geändert" % current_user.uid
            message = u"Nutzer %(name)s (%(uid)s) hat seine/ihre MAC-Adresse " \
                      u"geändert.\nAlte MAC: %(old_mac)s\nNeue MAC: %(new_mac)s" % \
                      { 'name': current_user.name, 'uid': current_user.uid,
                        'old_mac': userinfo['mac'], 'new_mac': mac }
            send_mail(current_user.uid + u"@wh2.tu-dresden.de",
                      "support@wh2.tu-dresden.de", subject, message)

            flash(gettext(u"MAC-Adresse wurde geändert!"), "success")
            return redirect(url_for('usersuite'))
        else:
            flash(gettext(u"Passwort war inkorrekt!"), "error")
    elif form.is_submitted():
        flash_formerrors(form)

    old_mac = userinfo['mac']
    return render_template('usersuite/change_mac.html',
                           form=form, old_mac=old_mac)


@app.route("/usertraffic")
def usertraffic():
    """For anonymous users with a valid IP
    """
    trafficdata = query_trafficdata(request.remote_addr)

    if trafficdata == -1:
        flash(gettext(u"Deine IP gehört nicht zum Wohnheim!"), "error")
        return redirect(url_for('index'))

    return render_template("usertraffic.html", usertraffic=trafficdata)


@app.route("/traffic.png")
def usersuite_trafficpng():
    """Create a traffic chart as png binary file and return the binary
    object to the client.

    If the user is not logged in, try to create a graph for the remote IP.
    Fails, if the IP was not recognized.
    """
    if current_user.is_anonymous():
        ip = request.remote_addr
    else:
        userinfo = query_userinfo(current_user.uid)
        ip = userinfo['ip']

    trafficdata = query_trafficdata(ip)
    if trafficdata == -1:
        flash(gettext(u"Es gab einen Fehler bei der Datenbankanfrage!"), "error")
        return redirect(url_for('index'))

    traffic_chart = make_trafficgraph(trafficdata)

    return send_file(io.BytesIO(traffic_chart.render_to_png()), "image/png")


@app.route("/bustimes")
@app.route("/bustimes/<string:stopname>")
def bustimes(stopname=None):
    """Queries the VVO-Online widget for the given stop.
    If no specific stop is given in the URL, it will query all
    stops set up in the config.
    """
    data = {}

    if stopname:
        # Only one stop requested
        data[stopname] = get_bustimes(stopname)
    else:
        # General output page
        for stop in busstops:
            data[stop] = get_bustimes(stop, 4)

    return render_template('bustimes.html', times=data, stopname=stopname)


if __name__ == "__main__":
    app.run(debug=True, host="localhost")
