import os
import Utils
import samba_utils
from samba_git import find_git

def git_version_summary(path, env=None):
    git = find_git(env)

    if git is None:
        return ("GIT-UNKNOWN", {})

    env.GIT = git

    environ = dict(os.environ)
    environ["GIT_DIR"] = '{0!s}/.git'.format(path)
    environ["GIT_WORK_TREE"] = path
    git = Utils.cmd_output(env.GIT + ' show --pretty=format:"%h%n%ct%n%H%n%cd" --stat HEAD', silent=True, env=environ)

    lines = git.splitlines()
    if not lines or len(lines) < 4:
        return ("GIT-UNKNOWN", {})

    fields = {
            "GIT_COMMIT_ABBREV": lines[0],
            "GIT_COMMIT_FULLREV": lines[2],
            "COMMIT_TIME": int(lines[1]),
            "COMMIT_DATE": lines[3],
            }

    ret = "GIT-" + fields["GIT_COMMIT_ABBREV"]

    if env.GIT_LOCAL_CHANGES:
        clean = Utils.cmd_output('{0!s} diff HEAD | wc -l'.format(env.GIT), silent=True).strip()
        if clean == "0":
            fields["COMMIT_IS_CLEAN"] = 1
        else:
            fields["COMMIT_IS_CLEAN"] = 0
            ret += "+"

    return (ret, fields)


def distversion_version_summary(path):
    #get version from .distversion file
    suffix = None
    fields = {}

    for line in Utils.readf(path + '/.distversion').splitlines():
        if line == '':
            continue
        if line.startswith("#"):
            continue
        try:
            split_line = line.split("=")
            if split_line[1] != "":
                key = split_line[0]
                value = split_line[1]
                if key == "SUFFIX":
                    suffix = value
                    continue
                fields[key] = value
        except:
            print("Failed to parse line {0!s} from .distversion file.".format((line)))
            raise

    if "COMMIT_TIME" in fields:
        fields["COMMIT_TIME"] = int(fields["COMMIT_TIME"])

    if suffix is None:
        return ("UNKNOWN", fields)

    return (suffix, fields)


class SambaVersion(object):

    def __init__(self, version_dict, path, env=None, is_install=True):
        '''Determine the version number of samba

See VERSION for the format.  Entries on that file are
also accepted as dictionary entries here
        '''

        self.MAJOR=None
        self.MINOR=None
        self.RELEASE=None
        self.REVISION=None
        self.TP_RELEASE=None
        self.ALPHA_RELEASE=None
        self.BETA_RELEASE=None
        self.PRE_RELEASE=None
        self.RC_RELEASE=None
        self.IS_SNAPSHOT=True
        self.RELEASE_NICKNAME=None
        self.VENDOR_SUFFIX=None
        self.VENDOR_PATCH=None

        for a, b in version_dict.iteritems():
            if a.startswith("SAMBA_VERSION_"):
                setattr(self, a[14:], b)
            else:
                setattr(self, a, b)

        if self.IS_GIT_SNAPSHOT == "yes":
            self.IS_SNAPSHOT=True
        elif self.IS_GIT_SNAPSHOT == "no":
            self.IS_SNAPSHOT=False
        else:
            raise Exception("Unknown value for IS_GIT_SNAPSHOT: {0!s}".format(self.IS_GIT_SNAPSHOT))

 ##
 ## start with "3.0.22"
 ##
        self.MAJOR=int(self.MAJOR)
        self.MINOR=int(self.MINOR)
        self.RELEASE=int(self.RELEASE)

        SAMBA_VERSION_STRING = ("{0:d}.{1:d}.{2:d}".format(self.MAJOR, self.MINOR, self.RELEASE))

##
## maybe add "3.0.22a" or "4.0.0tp11" or "4.0.0alpha1" or "4.0.0beta1" or "3.0.22pre1" or "3.0.22rc1"
## We do not do pre or rc version on patch/letter releases
##
        if self.REVISION is not None:
            SAMBA_VERSION_STRING += self.REVISION
        if self.TP_RELEASE is not None:
            self.TP_RELEASE = int(self.TP_RELEASE)
            SAMBA_VERSION_STRING += "tp{0:d}".format(self.TP_RELEASE)
        if self.ALPHA_RELEASE is not None:
            self.ALPHA_RELEASE = int(self.ALPHA_RELEASE)
            SAMBA_VERSION_STRING += ("alpha{0:d}".format(self.ALPHA_RELEASE))
        if self.BETA_RELEASE is not None:
            self.BETA_RELEASE = int(self.BETA_RELEASE)
            SAMBA_VERSION_STRING += ("beta{0:d}".format(self.BETA_RELEASE))
        if self.PRE_RELEASE is not None:
            self.PRE_RELEASE = int(self.PRE_RELEASE)
            SAMBA_VERSION_STRING += ("pre{0:d}".format(self.PRE_RELEASE))
        if self.RC_RELEASE is not None:
            self.RC_RELEASE = int(self.RC_RELEASE)
            SAMBA_VERSION_STRING += ("rc{0:d}".format(self.RC_RELEASE))

        if self.IS_SNAPSHOT:
            if not is_install:
                suffix = "DEVELOPERBUILD"
                self.vcs_fields = {}
            elif os.path.exists(os.path.join(path, ".git")):
                suffix, self.vcs_fields = git_version_summary(path, env=env)
            elif os.path.exists(os.path.join(path, ".distversion")):
                suffix, self.vcs_fields = distversion_version_summary(path)
            else:
                suffix = "UNKNOWN"
                self.vcs_fields = {}
            self.vcs_fields["SUFFIX"] = suffix
            SAMBA_VERSION_STRING += "-" + suffix
        else:
            self.vcs_fields = {}

        self.OFFICIAL_STRING = SAMBA_VERSION_STRING

        if self.VENDOR_SUFFIX is not None:
            SAMBA_VERSION_STRING += ("-" + self.VENDOR_SUFFIX)
            self.VENDOR_SUFFIX = self.VENDOR_SUFFIX

            if self.VENDOR_PATCH is not None:
                SAMBA_VERSION_STRING += ("-" + self.VENDOR_PATCH)
                self.VENDOR_PATCH = self.VENDOR_PATCH

        self.STRING = SAMBA_VERSION_STRING

        if self.RELEASE_NICKNAME is not None:
            self.STRING_WITH_NICKNAME = "{0!s} ({1!s})".format(self.STRING, self.RELEASE_NICKNAME)
        else:
            self.STRING_WITH_NICKNAME = self.STRING

    def __str__(self):
        string="/* Autogenerated by waf */\n"
        string+="#define SAMBA_VERSION_MAJOR {0:d}\n".format(self.MAJOR)
        string+="#define SAMBA_VERSION_MINOR {0:d}\n".format(self.MINOR)
        string+="#define SAMBA_VERSION_RELEASE {0:d}\n".format(self.RELEASE)
        if self.REVISION is not None:
            string+="#define SAMBA_VERSION_REVISION {0:d}\n".format(self.REVISION)

        if self.TP_RELEASE is not None:
            string+="#define SAMBA_VERSION_TP_RELEASE {0:d}\n".format(self.TP_RELEASE)

        if self.ALPHA_RELEASE is not None:
            string+="#define SAMBA_VERSION_ALPHA_RELEASE {0:d}\n".format(self.ALPHA_RELEASE)

        if self.BETA_RELEASE is not None:
            string+="#define SAMBA_VERSION_BETA_RELEASE {0:d}\n".format(self.BETA_RELEASE)

        if self.PRE_RELEASE is not None:
            string+="#define SAMBA_VERSION_PRE_RELEASE {0:d}\n".format(self.PRE_RELEASE)

        if self.RC_RELEASE is not None:
            string+="#define SAMBA_VERSION_RC_RELEASE {0:d}\n".format(self.RC_RELEASE)

        for name in sorted(self.vcs_fields.keys()):
            string+="#define SAMBA_VERSION_{0!s} ".format(name)
            value = self.vcs_fields[name]
            if isinstance(value, basestring):
                string += "\"{0!s}\"".format(value)
            elif type(value) is int:
                string += "{0:d}".format(value)
            else:
                raise Exception("Unknown type for {0!s}: {1!r}".format(name, value))
            string += "\n"

        string+="#define SAMBA_VERSION_OFFICIAL_STRING \"" + self.OFFICIAL_STRING + "\"\n"

        if self.VENDOR_SUFFIX is not None:
            string+="#define SAMBA_VERSION_VENDOR_SUFFIX " + self.VENDOR_SUFFIX + "\n"
            if self.VENDOR_PATCH is not None:
                string+="#define SAMBA_VERSION_VENDOR_PATCH " + self.VENDOR_PATCH + "\n"

        if self.RELEASE_NICKNAME is not None:
            string+="#define SAMBA_VERSION_RELEASE_NICKNAME " + self.RELEASE_NICKNAME + "\n"

        # We need to put this #ifdef in to the headers so that vendors can override the version with a function
        string+='''
#ifdef SAMBA_VERSION_VENDOR_FUNCTION
#  define SAMBA_VERSION_STRING SAMBA_VERSION_VENDOR_FUNCTION
#else /* SAMBA_VERSION_VENDOR_FUNCTION */
#  define SAMBA_VERSION_STRING "''' + self.STRING_WITH_NICKNAME + '''"
#endif
'''
        string+="/* Version for mkrelease.sh: \nSAMBA_VERSION_STRING=" + self.STRING_WITH_NICKNAME + "\n */\n"

        return string


def samba_version_file(version_file, path, env=None, is_install=True):
    '''Parse the version information from a VERSION file'''

    f = open(version_file, 'r')
    version_dict = {}
    for line in f:
        line = line.strip()
        if line == '':
            continue
        if line.startswith("#"):
            continue
        try:
            split_line = line.split("=")
            if split_line[1] != "":
                value = split_line[1].strip('"')
                version_dict[split_line[0]] = value
        except:
            print("Failed to parse line {0!s} from {1!s}".format(line, version_file))
            raise

    return SambaVersion(version_dict, path, env=env, is_install=is_install)



def load_version(env=None, is_install=True):
    '''load samba versions either from ./VERSION or git
    return a version object for detailed breakdown'''
    if not env:
        env = samba_utils.LOAD_ENVIRONMENT()

    version = samba_version_file("./VERSION", ".", env, is_install=is_install)
    Utils.g_module.VERSION = version.STRING
    return version
