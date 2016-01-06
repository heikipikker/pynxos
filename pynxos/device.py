import importlib
import signal
from .lib.rpc_client import RPCClient
from .lib import convert_dict_by_key, convert_list_by_key, list_from_table, converted_list_from_table, strip_unicode
from .lib.data_model import key_maps
from pynxos.features.file_copy import FileCopy
from pynxos.features.vlans import Vlans
from pynxos.errors import CLIError, NXOSError

class RebootSignal(NXOSError):
    pass

class Device(object):
    def __init__(self, host, username, password, transport=u'http', port=None, timeout=30):
        self.host = host
        self.username = username
        self.password = password
        self.transport = transport
        self.timeout = timeout

        self.rpc = RPCClient(host, username, password, transport=transport, port=port)

    def _cli_error_check(self, command_response):
        error = command_response.get(u'error')
        if error:
            command = command_response.get(u'command')
            if u'data' in error:
                raise CLIError(command, error[u'data'][u'msg'])
            else:
                raise CLIError(command, 'Invalid command.')

    def _cli_command(self, commands, method=u'cli'):
        if not isinstance(commands, list):
            commands = [commands]

        rpc_response = self.rpc.send_request(commands, method=method, timeout=self.timeout)

        text_response_list = []
        for command_response in rpc_response:
            self._cli_error_check(command_response)
            text_response_list.append(command_response[u'result'])

        return strip_unicode(text_response_list)

    def show(self, command, raw_text=False):
        commands = [command]
        list_result = self.show_list(commands, raw_text)
        if list_result:
            return list_result[0]
        else:
            return {}

    def show_list(self, commands, raw_text=False):
        return_list = []
        if raw_text:
            response_list = self._cli_command(commands, method=u'cli_ascii')
            for response in response_list:
                if response:
                    return_list.append(response[u'msg'])
        else:
            response_list = self._cli_command(commands)
            for response in response_list:
                if response:
                    return_list.append(response[u'body'])

        return return_list


    def config(self, command):
        commands = [command]
        list_result = self.config_list(commands)
        return list_result[0]

    def config_list(self, commands):
        return self._cli_command(commands)

    def save(self, filename='startup-config'):
        try:
            self.show(u'copy run %s' % filename, raw_text=True)
        except CLIError as e:
            if 'overwrite' in e.message:
                return False
            raise

        return True

    def stage_file_copy(self, src, dest=None):
        if dest is None:
            dest = src
        self.fc = FileCopy(self, src, dest)

    def file_copy_remote_exists(self):
        if self.fc.remote_file_exists():
            return True
        return False

    def file_copy(self):
        self.fc.send()

    def _disable_confirmation(self):
        self.show('terminal dont-ask')

    def reboot(self, confirm=False):
        if confirm:
            def handler(signum, frame):
                raise RebootSignal('Interupting after reload')

            signal.signal(signal.SIGALRM, handler)
            signal.alarm(5)

            try:
                self._disable_confirmation()
                self.show('reload')
            except RebootSignal:
                signal.alarm(0)

            signal.alarm(0)
        else:
            print('Need to confirm reboot with confirm=True')

    def install_os(self, image_name):
        output = self.show('install all nxos %s' % image_name, raw_text=True)
        self.save()
        return output

    def rollback(self, filename):
        self.show('rollback running-config file %s' % filename, raw_text=True)

    def checkpoint(self, filename):
        self.show('checkpoint file %s' % filename, raw_text=True)

    def backup_running_config(self, filename):
        with open(filename, 'w') as f:
            f.write(self.running_config)

    @property
    def running_config(self):
        response = self.show(u'show running-config', raw_text=True)
        return response

    def _convert_uptime_to_string(self, up_days, up_hours, up_mins, up_secs):
        return '%02d:%02d:%02d:%02d' % (up_days, up_hours, up_mins, up_secs)

    def _convert_uptime_to_seconds(self, up_days, up_hours, up_mins, up_secs):
        seconds = up_days * 24 * 60 * 60
        seconds += up_hours * 60 * 60
        seconds += up_mins * 60
        seconds += up_secs

        return seconds


    def _get_interface_detailed_list(self):
        try:
            interface_table = self.show(u'show interface status')
            interface_list = converted_list_from_table(interface_table, u'interface', key_maps.INTERFACE_KEY_MAP, fill_in=True)
        except CLIError:
            return []

        return interface_list

    def _get_interface_list(self):
        iface_detailed_list = self._get_interface_detailed_list()
        iface_list = list(x['interface'] for x in iface_detailed_list)

        return iface_list

    def _get_vlan_list(self):
        vlans = Vlans(self)
        vlan_list = vlans.get_list()

        return vlan_list


    def _get_show_version_facts(self):
        facts = {}
        show_version_result = self.show(u'show version')
        uptime_facts = convert_dict_by_key(show_version_result, key_maps.UPTIME_KEY_MAP)

        up_days = uptime_facts['up_days']
        up_hours = uptime_facts['up_hours']
        up_mins = uptime_facts['up_mins']
        up_secs = uptime_facts['up_secs']

        uptime_string = self._convert_uptime_to_string(up_days, up_hours, up_mins, up_secs)
        uptime_seconds = self._convert_uptime_to_seconds(up_days, up_hours, up_mins, up_secs)

        facts['uptime'] = uptime_seconds
        facts['uptime_string'] = uptime_string

        show_version_facts = convert_dict_by_key(show_version_result, key_maps.BASIC_FACTS_KEY_MAP)

        return show_version_facts


    @property
    def facts(self):
        '''
        '''
        if hasattr(self, '_facts'):
            return self._facts

        facts = {}

        show_version_facts = self._get_show_version_facts()
        facts.update(show_version_facts)

        iface_list = self._get_interface_list()
        facts['interfaces'] = iface_list

        vlan_list = self._get_vlan_list()
        facts['vlans'] = vlan_list

        self._facts = facts
        return facts

    def feature(self, feature_name):
        try:
            feature_module = importlib.import_module(
                'pynxos.features.%s' % (feature_name))
            return feature_module.instance(self)
        except ImportError:
            raise FeatureNotFoundError(feature_name, self.device_type)
