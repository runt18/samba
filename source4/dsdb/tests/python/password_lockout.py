#!/usr/bin/env python
# -*- coding: utf-8 -*-
# This tests the password lockout behavior for AD implementations
#
# Copyright Matthias Dieter Wallnoefer 2010
# Copyright Andrew Bartlett 2013
# Copyright Stefan Metzmacher 2014
#

import optparse
import sys
import base64
import time

sys.path.insert(0, "bin/python")
import samba

from samba.tests.subunitrun import TestProgram, SubunitOptions

import samba.getopt as options

from samba.auth import system_session
from samba.credentials import Credentials, DONT_USE_KERBEROS, MUST_USE_KERBEROS
from ldb import SCOPE_BASE, LdbError
from ldb import ERR_CONSTRAINT_VIOLATION
from ldb import ERR_INVALID_CREDENTIALS
from ldb import Message, MessageElement, Dn
from ldb import FLAG_MOD_REPLACE
from samba import gensec, dsdb
from samba.samdb import SamDB
import samba.tests
from samba.tests import delete_force
from samba.dcerpc import security, samr
from samba.ndr import ndr_unpack

parser = optparse.OptionParser("passwords.py [options] <host>")
sambaopts = options.SambaOptions(parser)
parser.add_option_group(sambaopts)
parser.add_option_group(options.VersionOptions(parser))
# use command line creds if available
credopts = options.CredentialsOptions(parser)
parser.add_option_group(credopts)
subunitopts = SubunitOptions(parser)
parser.add_option_group(subunitopts)
opts, args = parser.parse_args()

if len(args) < 1:
    parser.print_usage()
    sys.exit(1)

host = args[0]

lp = sambaopts.get_loadparm()
global_creds = credopts.get_credentials(lp)

# Force an encrypted connection
global_creds.set_gensec_features(global_creds.get_gensec_features() |
                                 gensec.FEATURE_SEAL)

def insta_creds(template=global_creds):
    # get a copy of the global creds or a the passed in creds
    c = Credentials()
    c.set_username("testuser")
    c.set_password("thatsAcomplPASS1")
    c.set_domain(template.get_domain())
    c.set_realm(template.get_realm())
    c.set_workstation(template.get_workstation())
    c.set_gensec_features(c.get_gensec_features()
                          | gensec.FEATURE_SEAL)
    c.set_kerberos_state(template.get_kerberos_state())
    return c

#
# Tests start here
#

class PasswordTests(samba.tests.TestCase):

    def _open_samr_user(self, res):
        self.assertTrue("objectSid" in res[0])

        (domain_sid, rid) = ndr_unpack(security.dom_sid, res[0]["objectSid"][0]).split()
        self.assertEquals(self.domain_sid, domain_sid)

        return self.samr.OpenUser(self.samr_domain, security.SEC_FLAG_MAXIMUM_ALLOWED, rid)

    def _reset_samr(self, res):

        # Now reset the lockout, by removing ACB_AUTOLOCK (which removes the lock, despite being a generated attribute)
        samr_user = self._open_samr_user(res)
        acb_info = self.samr.QueryUserInfo(samr_user, 16)
        acb_info.acct_flags &= ~samr.ACB_AUTOLOCK
        self.samr.SetUserInfo(samr_user, 16, acb_info)
        self.samr.Close(samr_user)

    def _reset_ldap_lockoutTime(self, res):
        self.ldb.modify_ldif("""
dn: """ + str(res[0].dn) + """
changetype: modify
replace: lockoutTime
lockoutTime: 0
""")

    def _reset_ldap_userAccountControl(self, res):
        self.assertTrue("userAccountControl" in res[0])
        self.assertTrue("msDS-User-Account-Control-Computed" in res[0])

        uac = int(res[0]["userAccountControl"][0])
        uacc = int(res[0]["msDS-User-Account-Control-Computed"][0])

        uac |= uacc
        uac = uac & ~dsdb.UF_LOCKOUT

        self.ldb.modify_ldif("""
dn: """ + str(res[0].dn) + """
changetype: modify
replace: userAccountControl
userAccountControl: {0:d}
""".format(uac))

    def _reset_by_method(self, res, method):
        if method is "ldap_userAccountControl":
            self._reset_ldap_userAccountControl(res)
        elif method is "ldap_lockoutTime":
            self._reset_ldap_lockoutTime(res)
        elif method is "samr":
            self._reset_samr(res)
        else:
            self.assertTrue(False, msg="Invalid reset method[{0!s}]".format(method))

    def _check_attribute(self, res, name, value):
        if value is None:
            self.assertTrue(name not in res[0],
                            msg="attr[{0!s}]={1!r} on dn[{2!s}]".format(name, res[0], res[0].dn))
            return

        if isinstance(value, tuple):
            (mode, value) = value
        else:
            mode = "equal"

        if mode == "ignore":
            return

        if mode == "absent":
            self.assertFalse(name in res[0],
                            msg="attr[{0!s}] not missing on dn[{1!s}]".format(name, res[0].dn))
            return

        self.assertTrue(name in res[0],
                        msg="attr[{0!s}] missing on dn[{1!s}]".format(name, res[0].dn))
        self.assertTrue(len(res[0][name]) == 1,
                        msg="attr[{0!s}]={1!r} on dn[{2!s}]".format(name, res[0][name], res[0].dn))


        print  "{0!s} = '{1!s}'".format(name, res[0][name][0])

        if mode == "present":
            return

        if mode == "equal":
            v = int(res[0][name][0])
            value = int(value)
            msg = ("attr[%s]=[%s] != [%s] on dn[%s]\n"
                   "(diff %d; actual value is %s than expected)"  %
                   (name, v, value, res[0].dn, v - value,
                    ('less' if v < value else 'greater')))

            self.assertTrue(v == value, msg)
            return

        if mode == "greater":
            v = int(res[0][name][0])
            self.assertTrue(v > int(value),
                            msg="attr[{0!s}]=[{1!s}] <= [{2!s}] on dn[{3!s}] (diff {4:d})".format(name, v, int(value), res[0].dn, v - int(value)))
            return
        if mode == "less":
            v = int(res[0][name][0])
            self.assertTrue(v < int(value),
                            msg="attr[{0!s}]=[{1!s}] >= [{2!s}] on dn[{3!s}] (diff {4:d})".format(name, v, int(value), res[0].dn, v - int(value)))
            return
        self.assertEqual(mode, not mode, "Invalid Mode[{0!s}]".format(mode))

    def _check_account(self, dn,
                       badPwdCount=None,
                       badPasswordTime=None,
                       lastLogon=None,
                       lastLogonTimestamp=None,
                       lockoutTime=None,
                       userAccountControl=None,
                       msDSUserAccountControlComputed=None,
                       effective_bad_password_count=None,
                       msg=None):
        print '-=' * 36
        if msg is not None:
            print  "\033[01;32m {0!s} \033[00m\n".format(msg)
        attrs = [
           "objectSid",
           "badPwdCount",
           "badPasswordTime",
           "lastLogon",
           "lastLogonTimestamp",
           "lockoutTime",
           "userAccountControl",
           "msDS-User-Account-Control-Computed"
        ]

        # in order to prevent some time resolution problems we sleep for
        # 10 micro second
        time.sleep(0.01)

        res = self.ldb.search(dn, scope=SCOPE_BASE, attrs=attrs)
        self.assertTrue(len(res) == 1)
        self._check_attribute(res, "badPwdCount", badPwdCount)
        self._check_attribute(res, "badPasswordTime", badPasswordTime)
        self._check_attribute(res, "lastLogon", lastLogon)
        self._check_attribute(res, "lastLogonTimestamp", lastLogonTimestamp)
        self._check_attribute(res, "lockoutTime", lockoutTime)
        self._check_attribute(res, "userAccountControl", userAccountControl)
        self._check_attribute(res, "msDS-User-Account-Control-Computed",
                              msDSUserAccountControlComputed)

        samr_user = self._open_samr_user(res)
        uinfo3 = self.samr.QueryUserInfo(samr_user, 3)
        uinfo5 = self.samr.QueryUserInfo(samr_user, 5)
        uinfo16 = self.samr.QueryUserInfo(samr_user, 16)
        uinfo21 = self.samr.QueryUserInfo(samr_user, 21)
        self.samr.Close(samr_user)

        expected_acb_info = 0
        if userAccountControl & dsdb.UF_NORMAL_ACCOUNT:
            expected_acb_info |= samr.ACB_NORMAL
        if userAccountControl & dsdb.UF_ACCOUNTDISABLE:
            expected_acb_info |= samr.ACB_DISABLED
        if userAccountControl & dsdb.UF_PASSWD_NOTREQD:
            expected_acb_info |= samr.ACB_PWNOTREQ
        if msDSUserAccountControlComputed & dsdb.UF_LOCKOUT:
            expected_acb_info |= samr.ACB_AUTOLOCK
        if msDSUserAccountControlComputed & dsdb.UF_PASSWORD_EXPIRED:
            expected_acb_info |= samr.ACB_PW_EXPIRED

        expected_bad_password_count = 0
        if badPwdCount is not None:
            expected_bad_password_count = badPwdCount
        if effective_bad_password_count is None:
            effective_bad_password_count = expected_bad_password_count

        self.assertEquals(uinfo3.acct_flags, expected_acb_info)
        self.assertEquals(uinfo3.bad_password_count, expected_bad_password_count)

        self.assertEquals(uinfo5.acct_flags, expected_acb_info)
        self.assertEquals(uinfo5.bad_password_count, effective_bad_password_count)

        self.assertEquals(uinfo16.acct_flags, expected_acb_info)

        self.assertEquals(uinfo21.acct_flags, expected_acb_info)
        self.assertEquals(uinfo21.bad_password_count, effective_bad_password_count)

        # check LDAP again and make sure the samr.QueryUserInfo
        # doesn't have any impact.
        res2 = self.ldb.search(dn, scope=SCOPE_BASE, attrs=attrs)
        self.assertEquals(res[0], res2[0])

        # in order to prevent some time resolution problems we sleep for
        # 10 micro second
        time.sleep(0.01)
        return res

    def assertLoginFailure(self, url, creds, lp, errno=ERR_INVALID_CREDENTIALS):
        try:
            ldb = SamDB(url=url, credentials=creds, lp=lp)
            self.fail("Login unexpectedly succeeded")
        except LdbError, (num, msg):
            if errno is not None:
                self.assertEquals(num, errno, ("Login failed in the wrong way"
                                               "(got err %d, expected %d)" %
                                               (num, errno)))

    def setUp(self):
        super(PasswordTests, self).setUp()

        self.ldb = SamDB(url=host_url, session_info=system_session(lp),
                         credentials=global_creds, lp=lp)

        # Gets back the basedn
        base_dn = self.ldb.domain_dn()

        # Gets back the configuration basedn
        configuration_dn = self.ldb.get_config_basedn().get_linearized()

        # Get the old "dSHeuristics" if it was set
        dsheuristics = self.ldb.get_dsheuristics()

        # Reset the "dSHeuristics" as they were before
        self.addCleanup(self.ldb.set_dsheuristics, dsheuristics)

        res = self.ldb.search(base_dn,
                         scope=SCOPE_BASE, attrs=["lockoutDuration", "lockOutObservationWindow", "lockoutThreshold"])

        if "lockoutDuration" in res[0]:
            lockoutDuration = res[0]["lockoutDuration"][0]
        else:
            lockoutDuration = 0

        if "lockoutObservationWindow" in res[0]:
            lockoutObservationWindow = res[0]["lockoutObservationWindow"][0]
        else:
            lockoutObservationWindow = 0

        if "lockoutThreshold" in res[0]:
            lockoutThreshold = res[0]["lockoutThreshold"][0]
        else:
            lockoutTreshold = 0

        self.addCleanup(self.ldb.modify_ldif, """
dn: """ + base_dn + """
changetype: modify
replace: lockoutDuration
lockoutDuration: """ + str(lockoutDuration) + """
replace: lockoutObservationWindow
lockoutObservationWindow: """ + str(lockoutObservationWindow) + """
replace: lockoutThreshold
lockoutThreshold: """ + str(lockoutThreshold) + """
""")

        m = Message()
        m.dn = Dn(self.ldb, base_dn)

        self.account_lockout_duration = 10
        account_lockout_duration_ticks = -int(self.account_lockout_duration * (1e7))

        m["lockoutDuration"] = MessageElement(str(account_lockout_duration_ticks),
                                              FLAG_MOD_REPLACE, "lockoutDuration")

        account_lockout_threshold = 3
        m["lockoutThreshold"] = MessageElement(str(account_lockout_threshold),
                                               FLAG_MOD_REPLACE, "lockoutThreshold")

        self.lockout_observation_window = 5
        lockout_observation_window_ticks = -int(self.lockout_observation_window * (1e7))

        m["lockOutObservationWindow"] = MessageElement(str(lockout_observation_window_ticks),
                                                       FLAG_MOD_REPLACE, "lockOutObservationWindow")

        self.ldb.modify(m)

        # Set the "dSHeuristics" to activate the correct "userPassword" behaviour
        self.ldb.set_dsheuristics("000000001")

        # Get the old "minPwdAge"
        minPwdAge = self.ldb.get_minPwdAge()

        # Reset the "minPwdAge" as it was before
        self.addCleanup(self.ldb.set_minPwdAge, minPwdAge)

        # Set it temporarely to "0"
        self.ldb.set_minPwdAge("0")

        self.base_dn = self.ldb.domain_dn()

        self.domain_sid = security.dom_sid(self.ldb.get_domain_sid())
        self.samr = samr.samr("ncacn_ip_tcp:{0!s}[sign]".format(host), lp, global_creds)
        self.samr_handle = self.samr.Connect2(None, security.SEC_FLAG_MAXIMUM_ALLOWED)
        self.samr_domain = self.samr.OpenDomain(self.samr_handle, security.SEC_FLAG_MAXIMUM_ALLOWED, self.domain_sid)

        # (Re)adds the test user "testuser" with no password atm
        delete_force(self.ldb, "cn=testuser,cn=users," + self.base_dn)
        self.ldb.add({
             "dn": "cn=testuser,cn=users," + self.base_dn,
             "objectclass": "user",
             "sAMAccountName": "testuser"})

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=0,
                                  lastLogon=0,
                                  lastLogonTimestamp=('absent', None),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT |
                                    dsdb.UF_ACCOUNTDISABLE |
                                    dsdb.UF_PASSWD_NOTREQD,
                                  msDSUserAccountControlComputed=
                                    dsdb.UF_PASSWORD_EXPIRED)

        # SAMR doesn't have any impact if dsdb.UF_LOCKOUT isn't present.
        # It doesn't create "lockoutTime" = 0.
        self._reset_samr(res)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=0,
                                  lastLogon=0,
                                  lastLogonTimestamp=('absent', None),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT |
                                    dsdb.UF_ACCOUNTDISABLE |
                                    dsdb.UF_PASSWD_NOTREQD,
                                  msDSUserAccountControlComputed=
                                    dsdb.UF_PASSWORD_EXPIRED)

        # Tests a password change when we don't have any password yet with a
        # wrong old password
        try:
            self.ldb.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: noPassword
add: userPassword
userPassword: thatsAcomplPASS2
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            # Windows (2008 at least) seems to have some small bug here: it
            # returns "0000056A" on longer (always wrong) previous passwords.
            self.assertTrue('00000056' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", 0),
                                  lastLogon=0,
                                  lastLogonTimestamp=('absent', None),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT |
                                    dsdb.UF_ACCOUNTDISABLE |
                                    dsdb.UF_PASSWD_NOTREQD,
                                  msDSUserAccountControlComputed=
                                    dsdb.UF_PASSWORD_EXPIRED)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # Sets the initial user password with a "special" password change
        # I think that this internally is a password set operation and it can
        # only be performed by someone which has password set privileges on the
        # account (at least in s4 we do handle it like that).
        self.ldb.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
add: userPassword
userPassword: thatsAcomplPASS1
""")

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=0,
                                  lastLogonTimestamp=('absent', None),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT |
                                    dsdb.UF_ACCOUNTDISABLE |
                                    dsdb.UF_PASSWD_NOTREQD,
                                  msDSUserAccountControlComputed=0)

        # Enables the user account
        self.ldb.enable_account("(sAMAccountName=testuser)")

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=0,
                                  lastLogonTimestamp=('absent', None),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # Open a second LDB connection with the user credentials. Use the
        # command line credentials for informations like the domain, the realm
        # and the workstation.
        creds2 = insta_creds()

        self.ldb2 = SamDB(url=host_url, credentials=creds2, lp=lp)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=('greater', 0),
                                  lastLogonTimestamp=('greater', 0),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        lastLogon = int(res[0]["lastLogon"][0])
        self.assertGreater(lastLogon, badPasswordTime)

     # (Re)adds the test user "testuser3" with no password atm
        delete_force(self.ldb, "cn=testuser3,cn=users," + self.base_dn)
        self.ldb.add({
             "dn": "cn=testuser3,cn=users," + self.base_dn,
             "objectclass": "user",
             "sAMAccountName": "testuser3"})

        res = self._check_account("cn=testuser3,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=0,
                                  lastLogon=0,
                                  lastLogonTimestamp=('absent', None),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT |
                                    dsdb.UF_ACCOUNTDISABLE |
                                    dsdb.UF_PASSWD_NOTREQD,
                                  msDSUserAccountControlComputed=
                                    dsdb.UF_PASSWORD_EXPIRED)

        # Tests a password change when we don't have any password yet with a
        # wrong old password
        try:
            self.ldb.modify_ldif("""
dn: cn=testuser3,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: noPassword
add: userPassword
userPassword: thatsAcomplPASS2
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            # Windows (2008 at least) seems to have some small bug here: it
            # returns "0000056A" on longer (always wrong) previous passwords.
            self.assertTrue('00000056' in msg)

        res = self._check_account("cn=testuser3,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", 0),
                                  lastLogon=0,
                                  lastLogonTimestamp=('absent', None),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT |
                                    dsdb.UF_ACCOUNTDISABLE |
                                    dsdb.UF_PASSWD_NOTREQD,
                                  msDSUserAccountControlComputed=
                                    dsdb.UF_PASSWORD_EXPIRED)
        badPasswordTime3 = int(res[0]["badPasswordTime"][0])

        # Sets the initial user password with a "special" password change
        # I think that this internally is a password set operation and it can
        # only be performed by someone which has password set privileges on the
        # account (at least in s4 we do handle it like that).
        self.ldb.modify_ldif("""
dn: cn=testuser3,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
add: userPassword
userPassword: thatsAcomplPASS1
""")

        res = self._check_account("cn=testuser3,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=badPasswordTime3,
                                  lastLogon=0,
                                  lastLogonTimestamp=('absent', None),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT |
                                    dsdb.UF_ACCOUNTDISABLE |
                                    dsdb.UF_PASSWD_NOTREQD,
                                  msDSUserAccountControlComputed=0)

        # Enables the user account
        self.ldb.enable_account("(sAMAccountName=testuser3)")

        res = self._check_account("cn=testuser3,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=badPasswordTime3,
                                  lastLogon=0,
                                  lastLogonTimestamp=('absent', None),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # Open a second LDB connection with the user credentials. Use the
        # command line credentials for informations like the domain, the realm
        # and the workstation.
        creds3 = insta_creds()
        creds3.set_username("testuser3")
        creds3.set_password("thatsAcomplPASS1")
        self.ldb3 = SamDB(url=host_url, credentials=creds3, lp=lp)

        res = self._check_account("cn=testuser3,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime3,
                                  lastLogon=('greater', badPasswordTime3),
                                  lastLogonTimestamp=('greater', badPasswordTime3),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

    def _test_userPassword_lockout_with_clear_change(self, method):
        print "Performs a password cleartext change operation on 'userPassword'"
        # Notice: This works only against Windows if "dSHeuristics" has been set
        # properly

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=("greater", 0),
                                  lastLogon=('greater', 0),
                                  lastLogonTimestamp=('greater', 0),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])
        lastLogon = int(res[0]["lastLogon"][0])
        lastLogonTimestamp = int(res[0]["lastLogonTimestamp"][0])

        # Change password on a connection as another user

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS1x
add: userPassword
userPassword: thatsAcomplPASS2
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertTrue('00000056' in msg)
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # Correct old password
        self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS1
add: userPassword
userPassword: thatsAcomplPASS2
""")

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS1x
add: userPassword
userPassword: thatsAcomplPASS2
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertTrue('00000056' in msg)
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=2,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        print "two failed password change"

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS1x
add: userPassword
userPassword: thatsAcomplPASS2
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertTrue('00000056' in msg)
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=("greater", badPasswordTime),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)
        badPasswordTime = int(res[0]["badPasswordTime"][0])
        lockoutTime = int(res[0]["lockoutTime"][0])

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS1x
add: userPassword
userPassword: thatsAcomplPASS2
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('00000775' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS1x
add: userPassword
userPassword: thatsAcomplPASS2
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('00000775' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lockoutTime=lockoutTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        try:
            # Correct old password
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS2
add: userPassword
userPassword: thatsAcomplPASS2x
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('0000775' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        # Now reset the password, which does NOT change the lockout!
        self.ldb.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
replace: userPassword
userPassword: thatsAcomplPASS2
""")

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        try:
            # Correct old password
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS2
add: userPassword
userPassword: thatsAcomplPASS2x
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('0000775' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        m = Message()
        m.dn = Dn(self.ldb, "cn=testuser,cn=users," + self.base_dn)
        m["userAccountControl"] = MessageElement(
          str(dsdb.UF_LOCKOUT),
          FLAG_MOD_REPLACE, "userAccountControl")

        self.ldb.modify(m)

        # This shows that setting the UF_LOCKOUT flag alone makes no difference
        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        # This shows that setting the UF_LOCKOUT flag makes no difference
        try:
            # Correct old password
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2x\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('0000775' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lockoutTime=lockoutTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        self._reset_by_method(res, method)

        # Here bad password counts are reset without logon success.
        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lockoutTime=0,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # The correct password after doing the unlock

        self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2x\"".encode('utf-16-le')) + """
""")

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lockoutTime=0,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS1xyz
add: userPassword
userPassword: thatsAcomplPASS2XYZ
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('00000056' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lockoutTime=0,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: userPassword
userPassword: thatsAcomplPASS1xyz
add: userPassword
userPassword: thatsAcomplPASS2XYZ
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('00000056' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=2,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lockoutTime=0,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        self._reset_ldap_lockoutTime(res)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=0,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

    def test_userPassword_lockout_with_clear_change_ldap_userAccountControl(self):
        self._test_userPassword_lockout_with_clear_change("ldap_userAccountControl")

    def test_userPassword_lockout_with_clear_change_ldap_lockoutTime(self):
        self._test_userPassword_lockout_with_clear_change("ldap_lockoutTime")

    def test_userPassword_lockout_with_clear_change_samr(self):
        self._test_userPassword_lockout_with_clear_change("samr")


    def test_unicodePwd_lockout_with_clear_change(self):
        print "Performs a password cleartext change operation on 'unicodePwd'"

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=("greater", 0),
                                  lastLogon=("greater", 0),
                                  lastLogonTimestamp=("greater", 0),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])
        lastLogon = int(res[0]["lastLogon"][0])

        # Change password on a connection as another user

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS1x\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertTrue('00000056' in msg)
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # Correct old password
        self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS1\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
""")

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS1\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertTrue('00000056' in msg)
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=2,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # SAMR doesn't have any impact if dsdb.UF_LOCKOUT isn't present.
        # It doesn't create "lockoutTime" = 0 and doesn't
        # reset "badPwdCount" = 0.
        self._reset_samr(res)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=2,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        print "two failed password change"

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS1x\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertTrue('00000056' in msg)
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)

        # this is strange, why do we have lockoutTime=badPasswordTime here?
        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=("greater", badPasswordTime),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)
        badPasswordTime = int(res[0]["badPasswordTime"][0])
        lockoutTime = int(res[0]["lockoutTime"][0])

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS1x\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('00000775' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS1x\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('00000775' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        try:
            # Correct old password
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2x\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)
            self.assertTrue('0000775' in msg)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        # Now reset the lockout, by removing ACB_AUTOLOCK (which removes the lock, despite being a generated attribute)
        self._reset_samr(res);

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=0,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # Correct old password
        self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2x\"".encode('utf-16-le')) + """
""")

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=0,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS1x\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertTrue('00000056' in msg)
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=0,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS1x\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertTrue('00000056' in msg)
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=2,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=0,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # SAMR doesn't have any impact if dsdb.UF_LOCKOUT isn't present.
        # It doesn't reset "badPwdCount" = 0.
        self._reset_samr(res)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=2,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=0,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # Wrong old password
        try:
            self.ldb3.modify_ldif("""
dn: cn=testuser,cn=users,""" + self.base_dn + """
changetype: modify
delete: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS1x\"".encode('utf-16-le')) + """
add: unicodePwd
unicodePwd:: """ + base64.b64encode("\"thatsAcomplPASS2\"".encode('utf-16-le')) + """
""")
            self.fail()
        except LdbError, (num, msg):
            self.assertTrue('00000056' in msg)
            self.assertEquals(num, ERR_CONSTRAINT_VIOLATION)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=("greater", badPasswordTime),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)
        badPasswordTime = int(res[0]["badPasswordTime"][0])
        lockoutTime = int(res[0]["lockoutTime"][0])

        time.sleep(self.account_lockout_duration + 1)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3, effective_bad_password_count=0,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # SAMR doesn't have any impact if dsdb.UF_LOCKOUT isn't present.
        # It doesn't reset "lockoutTime" = 0 and doesn't
        # reset "badPwdCount" = 0.
        self._reset_samr(res)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3, effective_bad_password_count=0,
                                  badPasswordTime=badPasswordTime,
                                  lockoutTime=lockoutTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogon,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

    def _test_login_lockout(self, use_kerberos):
        # This unlocks by waiting for account_lockout_duration
        if use_kerberos == MUST_USE_KERBEROS:
            lastlogon_relation = 'greater'
            print "Performs a lockout attempt against LDAP using Kerberos"
        else:
            lastlogon_relation = 'equal'
            print "Performs a lockout attempt against LDAP using NTLM"

        # Change password on a connection as another user
        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=("greater", 0),
                                  lastLogon=("greater", 0),
                                  lastLogonTimestamp=("greater", 0),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])
        lastLogon = int(res[0]["lastLogon"][0])
        firstLogon = lastLogon
        lastLogonTimestamp = int(res[0]["lastLogonTimestamp"][0])
        print firstLogon
        print lastLogonTimestamp


        self.assertGreater(lastLogon, badPasswordTime)

        # Open a second LDB connection with the user credentials. Use the
        # command line credentials for informations like the domain, the realm
        # and the workstation.
        creds_lockout = insta_creds()
        creds_lockout.set_kerberos_state(use_kerberos)

        # The wrong password
        creds_lockout.set_password("thatsAcomplPASS1x")

        self.assertLoginFailure(host_url, creds_lockout, lp)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0,
                                  msg='lastlogontimestamp with wrong password')
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # Correct old password
        creds_lockout.set_password("thatsAcomplPASS1")

        ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)

        # lastLogonTimestamp should not change
        # lastLogon increases if badPwdCount is non-zero (!)
        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=('greater', lastLogon),
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0,
                                  msg='LLTimestamp is updated to lastlogon')

        lastLogon = int(res[0]["lastLogon"][0])
        self.assertGreater(lastLogon, badPasswordTime)

        # The wrong password
        creds_lockout.set_password("thatsAcomplPASS1x")

        self.assertLoginFailure(host_url, creds_lockout, lp)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # The wrong password
        creds_lockout.set_password("thatsAcomplPASS1x")

        try:
            ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)
            self.fail()

        except LdbError, (num, msg):
            self.assertEquals(num, ERR_INVALID_CREDENTIALS)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=2,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        print "two failed password change"

        # The wrong password
        creds_lockout.set_password("thatsAcomplPASS1x")

        try:
            ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)
            self.fail()

        except LdbError, (num, msg):
            self.assertEquals(num, ERR_INVALID_CREDENTIALS)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=("greater", badPasswordTime),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)
        badPasswordTime = int(res[0]["badPasswordTime"][0])
        lockoutTime = int(res[0]["lockoutTime"][0])

        # The wrong password
        creds_lockout.set_password("thatsAcomplPASS1x")
        try:
            ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_INVALID_CREDENTIALS)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        # The wrong password
        creds_lockout.set_password("thatsAcomplPASS1x")
        try:
            ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_INVALID_CREDENTIALS)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        # The correct password, but we are locked out
        creds_lockout.set_password("thatsAcomplPASS1")
        try:
            ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_INVALID_CREDENTIALS)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=lockoutTime,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=dsdb.UF_LOCKOUT)

        # wait for the lockout to end
        time.sleep(self.account_lockout_duration + 1)
        print self.account_lockout_duration + 1

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=3, effective_bad_password_count=0,
                                  badPasswordTime=badPasswordTime,
                                  lockoutTime=lockoutTime,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        lastLogon = int(res[0]["lastLogon"][0])

        # The correct password after letting the timeout expire

        creds_lockout.set_password("thatsAcomplPASS1")

        creds_lockout2 = insta_creds(creds_lockout)

        ldb_lockout = SamDB(url=host_url, credentials=creds_lockout2, lp=lp)
        time.sleep(3)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=(lastlogon_relation, lastLogon),
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  lockoutTime=0,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0,
                                  msg="lastLogon is way off")

        lastLogon = int(res[0]["lastLogon"][0])

        # The wrong password
        creds_lockout.set_password("thatsAcomplPASS1x")
        try:
            ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_INVALID_CREDENTIALS)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lockoutTime=0,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # The wrong password
        creds_lockout.set_password("thatsAcomplPASS1x")
        try:
            ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_INVALID_CREDENTIALS)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=2,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lockoutTime=0,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        time.sleep(self.lockout_observation_window + 1)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=2, effective_bad_password_count=0,
                                  badPasswordTime=badPasswordTime,
                                  lockoutTime=0,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

        # The wrong password
        creds_lockout.set_password("thatsAcomplPASS1x")
        try:
            ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)
            self.fail()
        except LdbError, (num, msg):
            self.assertEquals(num, ERR_INVALID_CREDENTIALS)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=1,
                                  badPasswordTime=("greater", badPasswordTime),
                                  lockoutTime=0,
                                  lastLogon=lastLogon,
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])

        # The correct password without letting the timeout expire
        creds_lockout.set_password("thatsAcomplPASS1")
        ldb_lockout = SamDB(url=host_url, credentials=creds_lockout, lp=lp)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lockoutTime=0,
                                  lastLogon=("greater", lastLogon),
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

    def test_login_lockout_ntlm(self):
        self._test_login_lockout(DONT_USE_KERBEROS)

    def test_login_lockout_kerberos(self):
        self._test_login_lockout(MUST_USE_KERBEROS)

    def _test_multiple_logon(self, use_kerberos):
        # Test the happy case in which a user logs on correctly, then
        # logs on correctly again, so that the bad password and
        # lockout times are both zero the second time. The lastlogon
        # time should increase.

        # Open a second LDB connection with the user credentials. Use the
        # command line credentials for informations like the domain, the realm
        # and the workstation.
        creds2 = insta_creds()
        creds2.set_kerberos_state(use_kerberos)
        self.assertEqual(creds2.get_kerberos_state(), use_kerberos)

        if use_kerberos == MUST_USE_KERBEROS:
            print "Testing multiple logon with Kerberos"
            lastlogon_relation = 'greater'
        else:
            print "Testing multiple logon with NTLM"
            lastlogon_relation = 'equal'

        SamDB(url=host_url, credentials=insta_creds(creds2), lp=lp)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=("greater", 0),
                                  lastLogon=("greater", 0),
                                  lastLogonTimestamp=("greater", 0),
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)
        badPasswordTime = int(res[0]["badPasswordTime"][0])
        lastLogon = int(res[0]["lastLogon"][0])
        lastLogonTimestamp = int(res[0]["lastLogonTimestamp"][0])
        firstLogon = lastLogon
        print "last logon is {0:d}".format(lastLogon)
        self.assertGreater(lastLogon, badPasswordTime)

        time.sleep(1)
        SamDB(url=host_url, credentials=insta_creds(creds2), lp=lp)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=(lastlogon_relation, lastLogon),
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                  dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0,
                                  msg=("second logon, firstlogon was {0!s}".format(
                                       firstLogon)))


        lastLogon = int(res[0]["lastLogon"][0])

        time.sleep(1)

        SamDB(url=host_url, credentials=insta_creds(creds2), lp=lp)

        res = self._check_account("cn=testuser,cn=users," + self.base_dn,
                                  badPwdCount=0,
                                  badPasswordTime=badPasswordTime,
                                  lastLogon=(lastlogon_relation, lastLogon),
                                  lastLogonTimestamp=lastLogonTimestamp,
                                  userAccountControl=
                                    dsdb.UF_NORMAL_ACCOUNT,
                                  msDSUserAccountControlComputed=0)

    def test_multiple_logon_ntlm(self):
        self._test_multiple_logon(DONT_USE_KERBEROS)

    def test_multiple_logon_kerberos(self):
        self._test_multiple_logon(MUST_USE_KERBEROS)

    def tearDown(self):
        super(PasswordTests, self).tearDown()
        delete_force(self.ldb, "cn=testuser,cn=users," + self.base_dn)
        delete_force(self.ldb, "cn=testuser2,cn=users," + self.base_dn)
        delete_force(self.ldb, "cn=testuser3,cn=users," + self.base_dn)
        # Close the second LDB connection (with the user credentials)
        self.ldb2 = None

host_url = "ldap://{0!s}".format(host)

TestProgram(module=__name__, opts=subunitopts)
