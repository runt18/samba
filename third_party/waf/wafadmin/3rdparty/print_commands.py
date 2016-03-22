#! /usr/bin/env python

"""
In this case, print the commands being executed as strings
(the commands are usually lists, so this can be misleading)
"""

import Build, Utils, Logs

def exec_command(self, cmd, **kw):
	txt = cmd
	if isinstance(cmd, list):
		txt = ' '.join(cmd)
	Logs.debug('runner: {0!s}'.format(txt))
	if self.log:
		self.log.write('{0!s}\n'.format(cmd))
		kw['log'] = self.log
	try:
		if not kw.get('cwd', None):
			kw['cwd'] = self.cwd
	except AttributeError:
		self.cwd = kw['cwd'] = self.bldnode.abspath()
	return Utils.exec_command(cmd, **kw)
Build.BuildContext.exec_command = exec_command

