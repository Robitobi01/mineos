#!/usr/bin/python3

import inspect
import os
import types
from collections import defaultdict
from grp import getgrall
from grp import getgrgid
from grp import getgrnam
from json import loads
from pwd import getpwnam
from pwd import getpwuid
from subprocess import CalledProcessError

import cherrypy
from cherrypy.lib.static import serve_file

import procfs_reader
from auth import require
from mineos import mc
from procfs_reader import proc_loadavg


def strongly_expire(func):
    def newfunc(*args, **kwargs):
        cherrypy.response.headers['Expires'] = 'Sun, 19 Nov 1978 05:00:00 GMT'
        cherrypy.response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0'
        cherrypy.response.headers['Pragma'] = 'no-cache'
        return func(*args, **kwargs)

    return newfunc


def to_jsonable_type(retval):
    if isinstance(retval, types.GeneratorType):
        return list(retval)
    elif hasattr(retval, '__dict__'):
        return dict(retval.__dict__)
    else:
        return retval


def exception_msg(ex):
    if hasattr(ex, 'message'):
        return ex.message
    elif hasattr(ex, 'output'):
        return ex.output
    else:
        return str(ex)


class ViewModel(object):
    def __init__(self):
        self.base_directory = cherrypy.config['misc.base_directory']

    @property
    def login(self):
        return str(cherrypy.session['_cp_username'])

    def server_list(self):
        for i in mc.list_servers(self.base_directory):
            if mc.has_server_rights(self.login, i, self.base_directory):
                yield i

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    def status(self):
        servers = []
        for i in self.server_list():
            try:
                instance = mc(i, self.login, self.base_directory)
            except ValueError:
                continue  # fails valid_server_name

            try:
                java_xmx = int(instance.server_config['java':'java_xmx'])
            except (KeyError, ValueError):
                java_xmx = 0

            srv = {
                'server_name': i,
                'jarfile': instance.jarfile,
                'up': instance.up,
                'ip_address': instance.ip_address,
                'port': instance.port,
                'memory': instance.memory,
                'java_xmx': java_xmx,
                'eula': instance.eula
            }

            try:
                ping = instance.ping
            except KeyError:
                continue
            except IndexError:
                srv.update({
                    'protocol_version': '',
                    'server_version': '',
                    'motd': '',
                    'players_online': -1,
                    'max_players': instance.server_properties['max-players'::0]
                })
            else:
                srv.update(dict(instance.ping._asdict()))

            servers.append(srv)

        return servers


    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    def increments(self, server_name):
        instance = mc(server_name, self.login, self.base_directory)
        return [dict(d._asdict()) for d in instance.list_increment_sizes()]

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    def archives(self, server_name):
        instance = mc(server_name, self.login, self.base_directory)
        return [dict(d._asdict()) for d in instance.list_archives()]

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    def server_summary(self, server_name):

        cwd = os.path.join(self.base_directory, mc.DEFAULT_PATHS['servers'], server_name)
        bwd = os.path.join(self.base_directory, mc.DEFAULT_PATHS['backup'], server_name)
        awd = os.path.join(self.base_directory, mc.DEFAULT_PATHS['archive'], server_name)
        st = os.stat(cwd)

        dir_info = {
            'owner': getpwuid(st.st_uid).pw_name,
            'group': getgrgid(st.st_gid).gr_name
        }

        try:
            dir_info['du_cwd'] = procfs_reader.disk_usage(cwd)
        except:
            dir_info['du_cwd'] = 0

        return dir_info

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    def loadavg(self):

        return proc_loadavg()

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    def dashboard(self):

        kb_free = dict(procfs_reader.entries('', 'meminfo'))['MemAvailable']
        gb_free = str(round(float(kb_free.split()[0]) / 1000 / 1000, 3)) + ' GB'

        primary_group = getgrgid(getpwnam(self.login).pw_gid).gr_name

        return {
            'uptime': int(procfs_reader.proc_uptime()[0]),
            'memfree': gb_free,
            'whoami': self.login,
            'group': primary_group,
            'df': dict(procfs_reader.disk_free(cherrypy.config['misc.base_directory'])._asdict()),
            'groups': [i.gr_name for i in getgrall() if self.login in i.gr_mem or self.login == 'root'] + [
                primary_group],
            'base_directory': self.base_directory,
        }

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    def importable(self):
        path = os.path.join(self.base_directory, mc.DEFAULT_PATHS['import'])
        return [{
            'path': path,
            'filename': f
        } for f in mc._list_files(path)]


class Root(object):
    METHODS = list(m for m in dir(mc) if callable(getattr(mc, m)) \
                   and not m.startswith('_'))
    PROPERTIES = list(m for m in dir(mc) if not callable(getattr(mc, m)) \
                      and not m.startswith('_'))

    def __init__(self):
        self.html_directory = cherrypy.config['misc.html_directory']
        self.base_directory = cherrypy.config['misc.base_directory']

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    def webui_config(self):
        return {k.lstrip('webui.'): v for k, v in cherrypy.config.items() if k.startswith('webui.')}

    @property
    def login(self):
        return str(cherrypy.session['_cp_username'])

    @cherrypy.expose
    @require()
    def index(self):
        return serve_file(os.path.join(self.html_directory, 'index.html'))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    @require()
    def host(self, **raw_args):
        args = {k: str(v) for k, v in raw_args.items()}
        command = args.pop('cmd')
        retval = None

        response = {
            'result': None,
            'cmd': command,
            'payload': None
        }

        try:
            if command in self.METHODS:
                try:
                    if 'base_directory' in inspect.getargspec(getattr(mc, command)).args:
                        retval = getattr(mc, command)(base_directory=init_args['base_directory'], **args)
                    else:
                        retval = getattr(mc, command)(**args)
                except TypeError as ex:
                    raise RuntimeError(exception_msg(ex))
            else:
                raise RuntimeWarning('Command not found: should this be to a server?')
        except (RuntimeError, KeyError, OSError, NotImplementedError) as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except CalledProcessError as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except RuntimeWarning as ex:
            response['result'] = 'warning'
            retval = exception_msg(ex)
        else:
            response['result'] = 'success'

        response['payload'] = to_jsonable_type(retval)
        return response

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    @require()
    def server(self, **raw_args):
        args = {k: str(v) for k, v in raw_args.items()}
        command = args.pop('cmd')
        server_name = args.pop('server_name')
        retval = None

        response = {
            'result': None,
            'cmd': command,
            'payload': None
        }

        owner = mc.has_server_rights(self.login, server_name, self.base_directory)

        try:
            if server_name is None:
                raise KeyError('Required value missing: server_name')
            elif not owner:
                raise OSError('User %s does not have permissions on %s' % (self.login, server_name))

            instance = mc(server_name, owner, self.base_directory)

            if command in self.METHODS:
                retval = getattr(instance, command)(**args)
            elif command in self.PROPERTIES:
                if args:
                    setattr(instance, command, list(args.values())[0])
                    retval = list(args.values())[0]
                else:
                    retval = getattr(instance, command)
            else:
                instance._command_stuff(command)
                retval = '"%s" successfully sent to server.' % command
        except (RuntimeError, KeyError, NotImplementedError) as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except CalledProcessError as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except RuntimeWarning as ex:
            response['result'] = 'warning'
            retval = exception_msg(ex)
        else:
            response['result'] = 'success'

        response['payload'] = to_jsonable_type(retval)
        return response

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    @require()
    def logs(self, **raw_args):
        args = {k: str(v) for k, v in raw_args.items()}
        server_name = args.pop('server_name')
        retval = None

        response = {
            'result': None,
            'cmd': 'logs',
            'payload': None
        }

        try:
            instance = mc(server_name, self.login, self.base_directory)

            if 'log_offset' not in cherrypy.session or 'reset' in args:
                cherrypy.session['log_offset'] = os.stat(instance.env['log']).st_size
                retval = instance.list_last_loglines(100)
            elif not cherrypy.session['log_offset']:
                cherrypy.session['log_offset'] = os.stat(instance.env['log']).st_size
                retval = instance.list_last_loglines(100)
            elif cherrypy.session['log_offset']:
                with open(instance.env['log'], 'r') as log:
                    log.seek(cherrypy.session['log_offset'], 0)
                    retval = log.readlines()
                    cherrypy.session['log_offset'] = os.stat(instance.env['log']).st_size
        except (RuntimeError, KeyError) as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except CalledProcessError as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except (RuntimeWarning, OSError) as ex:
            response['result'] = 'warning'
            retval = exception_msg(ex)
        else:
            response['result'] = 'success'

        response['payload'] = to_jsonable_type(retval)
        return response

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    @require()
    def create(self, **raw_args):
        args = {k: str(v) for k, v in raw_args.items()}
        server_name = args.pop('server_name')
        group = args.pop('group', None)
        retval = None

        response = {
            'result': None,
            'cmd': 'create',
            'payload': None
        }

        try:
            group_info = None
            if group:
                try:
                    group_info = getgrnam(group)
                except KeyError:
                    raise KeyError("There is no group '%s'" % group)
                else:
                    if self.login not in group_info.gr_mem and self.login != group_info.gr_name:
                        raise OSError("user '%s' is not part of group '%s'" % (self.login, group))

            instance = mc(server_name, self.login, self.base_directory)
            sp_unicode = loads(args['sp'])
            sc_unicode = loads(args['sc'])

            sp = {str(k): str(v) for k, v in sp_unicode.items()}
            sc = defaultdict(dict)

            for section in list(sc_unicode.keys()):
                for key in list(sc_unicode[section].keys()):
                    sc[str(section)][str(key)] = str(sc_unicode[section][key])

            instance.create(dict(sc), sp)
            if group:
                for d in ('servers', 'backup', 'archive'):
                    path_ = os.path.join(self.base_directory, mc.DEFAULT_PATHS[d], server_name)
                    os.lchown(path_, -1, group_info.gr_gid)
        except (RuntimeError, KeyError, OSError, ValueError) as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except CalledProcessError as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except RuntimeWarning as ex:
            response['result'] = 'warning'
            retval = exception_msg(ex)
        else:
            response['result'] = 'success'

        response['payload'] = to_jsonable_type(retval)
        return response

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    @require()
    def import_server(self, **raw_args):
        args = {k: str(v) for k, v in raw_args.items()}
        server_name = args.pop('server_name')
        retval = None

        response = {
            'result': None,
            'cmd': 'import_server',
            'payload': None
        }

        try:
            instance = mc(server_name, self.login, self.base_directory)
            instance.import_server(**args)
            instance = mc(server_name, None, self.base_directory)
            instance.chown(self.login)
            instance.chgrp(getgrgid(getpwnam(self.login).pw_gid).gr_name)
        except (RuntimeError, KeyError, OSError, ValueError) as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except CalledProcessError as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except RuntimeWarning as ex:
            response['result'] = 'warning'
            retval = exception_msg(ex)
        else:
            response['result'] = 'success'
            retval = "Server '%s' successfully imported" % server_name

        response['payload'] = to_jsonable_type(retval)
        return response

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    @require()
    def change_group(self, **raw_args):
        args = {k: str(v) for k, v in raw_args.items()}
        server_name = args.pop('server_name')
        group = args.pop('group')
        retval = None

        response = {
            'result': None,
            'cmd': 'chgrp',
            'payload': None
        }

        try:
            if self.login == mc.has_server_rights(self.login, server_name, self.base_directory) or \
                    self.login == 'root':
                instance = mc(server_name, None, self.base_directory)
                instance.chgrp(group)
            else:
                raise OSError('Group assignment to %s failed. Only the owner make change groups.' % group)
        except (RuntimeError, KeyError, OSError) as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except CalledProcessError as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except RuntimeWarning as ex:
            response['result'] = 'warning'
            retval = exception_msg(ex)
        else:
            response['result'] = 'success'
            retval = "Server '%s' group ownership granted to '%s'" % (server_name, group)

        response['payload'] = to_jsonable_type(retval)
        return response


    @cherrypy.expose
    @cherrypy.tools.json_out()
    @strongly_expire
    @require()
    def delete_server(self, **raw_args):
        args = {k: str(v) for k, v in raw_args.items()}
        server_name = args.pop('server_name')
        retval = None

        response = {
            'result': None,
            'cmd': 'delete_server',
            'payload': None
        }

        try:
            if mc.has_server_rights(self.login, server_name, self.base_directory):
                instance = mc(server_name, None, self.base_directory)
                instance.delete_server()
            else:
                raise OSError('Server deletion failed. Only the server owner or root may delete servers.')
        except (RuntimeError, KeyError, OSError) as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except CalledProcessError as ex:
            response['result'] = 'error'
            retval = exception_msg(ex)
        except RuntimeWarning as ex:
            response['result'] = 'warning'
            retval = exception_msg(ex)
        else:
            response['result'] = 'success'
            retval = "Server '%s' deleted" % server_name

        response['payload'] = to_jsonable_type(retval)
        return response
