#!/usr/bin/env python

#
# Compare the results of native and cross-compiled configure tests
#

import sys
import difflib

exceptions = ['BUILD_DIRECTORY', 'CROSS_COMPILE', 'CROSS_ANSWERS',
              'CROSS_EXECUTE', 'SELFTEST_PREFIX']

base_lines = list()
base_fname = ''

found_diff = False

for fname in sys.argv[1:]:
    lines = list()
    f = open(fname, 'r')
    for line in f:
        if len(line.split('=', 1)) == 2:
            key = line.split('=', 1)[0].strip()
            if key in exceptions:
                continue
        lines.append(line)
    f.close()
    if base_fname:
        diff = list(difflib.unified_diff(base_lines,lines,base_fname,fname))
        if diff:
            print 'configuration files {0!s} and {1!s} do not match'.format(base_fname, fname)
            for l in diff:
                sys.stdout.write(l)
            found_diff = True
    else:
        base_fname = fname
        base_lines = lines

if found_diff:
    sys.exit(1)
