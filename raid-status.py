#!/usr/bin/env python3
"""Maintains RAIDs status of local system. Enumerates them and warns if something is wrong. Intended to be used together with Zabbix"""

import os,json,subprocess,re,optparse

class RAID():
    # Concrete classes register themselves and held in this variable
    __registry = {}
    
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "{}('{}')".format(self.__class__.__name__, self.name)

    def __str__(self):
        return "{},{}".format(self.slug, self.name)

    # Must be overridden
    @staticmethod
    def is_supported():
        """Says if this (sub)class could do any useful job by itself"""
        return False

    # Must be overridden; this implementation simply calls discover for all registered subclasses
    @classmethod
    def discover(cls):
        """Returns list of class instances which are capable of telling their attributes"""
        discovered = []
        for slug, rcls in cls.__registry.items():
            if rcls.is_supported():
                discovered += rcls.discover()
        return discovered

    # Must be overridden
    @property
    def status(self):
        """Returns array state: one of 'Optimal', 'Degraded', 'Critical', 'Reconstruction', 'Check', 'Unknown'"""
        return 'Unknown'
    
    @property
    def stable_name(self):
        """Returns stable name for this array"""
        return 'None'

    @classmethod
    def register(cls):
        """Registers this RAID type to the autodispatcher, please call <subclass>.register() after definition of <subclass>"""
        cls.__registry[cls.slug] = cls

    @classmethod
    def create(cls, serialized_name):
        """Factory, identifies concrete registered subclass by its slug in the serialized_name, then creates that <subclass> instance with the rest of serialized_name info"""
        data = serialized_name.split(',')
        slug = data.pop(0)
        if slug not in cls.__registry:
            raise ValueError("Handler for {} is not registered!".format(slug))
        return cls.__registry[slug](*data)

# Кэшировать ответы megacli и прочих! Возможно, это получится делать в классе, т.к. в пределах
# класса ответ один и мы его распарсиваем # и выдираем разные части в зависимости от аргумента
def parse_by_colon(string_to_parse, field_to_extract):
    """Helper used to extract data from megacli and mdadm outputs"""
    return [i.split(':', 1)[1].strip() for i in string_to_parse.split('\n') if i.split(':', 1)[0].strip() == field_to_extract][0]

class MegaRAID_SAS(RAID):
    """This class supports LSI/Avago MegaRAID SAS controllers with the help of megacli utility"""
    slug = 'megaraid_sas'

    def __init__(self, name):
        super().__init__(name)
        adapter, logical_disk = self.name.split(':')
        
    @staticmethod
    def is_supported():
        # TODO: check if system actually has compartible controller
        if not os.path.isfile("/usr/sbin/megacli"):
            return False
        if not os.access("/usr/sbin/megacli", os.X_OK):
            return False
        return True

    @classmethod
    def discover(cls):
        """Discovers all MegaRAID SAS virtual disks"""
        FNULL = open(os.devnull, 'w')
        # TODO: support for -aN and scanning controllers other that zero
        # This command prints a human-readable answer AND sets exit-code to the actual number, we use exit-code
        raids_count = subprocess.call(["/usr/sbin/megacli","-ldgetnum","-a0"], stdout=FNULL, stderr=subprocess.STDOUT)
        # Arrays are merely indexed, from 0 to raids_count-1. Numbers are stable, identify arrays unambigiously.
        return [cls("{}:{}".format(0, i)) for i in range(raids_count)]

    def __ldinfo(self):
        FNULL = open(os.devnull, 'w')
        adapter, logical_disk = self.name.split(':')
        result = subprocess.check_output(['/usr/sbin/megacli', '-ldinfo', '-l' + logical_disk, '-a' + adapter], stderr=FNULL)
        return result.decode('utf-8')
    
    @property
    def status(self):
        """Returns array state: one of 'Optimal', 'Degraded', 'Critical', 'Reconstruction', 'Check', 'Unknown'"""
        return parse_by_colon(self.__ldinfo(), "State")

    @property
    def stable_name(self):
        """Returns stable name for this array"""
        return parse_by_colon(self.__ldinfo(), "Name")

MegaRAID_SAS.register()

# TODO: fix everything to use subprocess.run in stead of check_output
class SSA(RAID):
    slug = 'ssa'

    XLATE_STATUS = {
        'OK': "Optimal",
    }

    @staticmethod
    def is_supported():
        # TODO: check if system actually has compartible controller
        if not os.path.isfile("/usr/sbin/ssacli"):
            return False
        if not os.access("/usr/sbin/ssacli", os.X_OK):
            return False
        return True

    @classmethod
    def discover(cls):
        """Discovers all HPE Smart Storage Array elements"""
        FNULL = open(os.devnull, 'w')
        result = subprocess.check_output(['/usr/sbin/ssacli', 'ctrl', 'all', 'show'], stderr=FNULL)

        ctrls=[]
        sare = re.compile('Smart Array .+ in Slot (\d+).+')
        for sa_line in result.decode('utf-8').split('\n'):
            m = sare.match(sa_line)
            if m:
                ctrls.append(m.group(1))

        arrays=[]
        are = re.compile('.*Array ([A-Z]) \(')
        for i in ctrls:
            result = subprocess.check_output(['/usr/sbin/ssacli', 'ctrl', 'slot='+i, 'array', 'all', 'show'], stderr=FNULL)
            for array_line in result.decode('utf-8').split('\n'):
                m = are.match(array_line)
                if m:
                    arrays.append((i,m.group(1)))

        return [cls("{}:{}".format(*i)) for i in arrays]

    @property
    def status(self):
        """Returns array state: one of 'Optimal', 'Degraded', 'Critical', 'Reconstruction', 'Check', 'Unknown'"""
        FNULL = open(os.devnull, 'w')
        slot, array = self.name.split(':')
        result = subprocess.check_output(['/usr/sbin/ssacli', 'ctrl', 'slot='+slot, 'array', array, 'show', 'detail'], stderr=FNULL)
        status = parse_by_colon(result.decode('utf-8'), "Status")
        return self.XLATE_STATUS.get(status, status)
    
    # No stable name is stored. It is unknown if C will ever become should A or B be deleted.
    @property
    def stable_name(self):
        return self.name

SSA.register()

class MD_RAID(RAID):
    slug = 'md_raid'
    
    XLATE_STATUS = {
        'clean': "Optimal",
        'active': "Optimal",
    }

    @staticmethod
    def is_supported():
        if not os.path.isfile("/proc/mdstat"):
            return False
        if not os.access("/proc/mdstat", os.R_OK):
            return False
        if not os.path.isfile("/sbin/mdadm"):
            return False
        if not os.access("/sbin/mdadm", os.X_OK):
            return False
        return True

    @classmethod
    def discover(cls):
        #"""Discovers all Linux MD RAID arrays"""
        #FNULL = open(os.devnull, 'w')
        #result = subprocess.run(['/sbin/mdadm', '--examine', '--scan'], stdout=subprocess.PIPE, stderr=FNULL)
        #return [cls(i.split(' ')[-1].split('=')[1]) for i in result.stdout.decode('utf-8').split('\n')[0:-1]]
        arrays=[]
        with open('/proc/mdstat', 'r') as mdstat_file:
            mdre = re.compile('^md(\d+)\s+:')
            for mdstat_line in mdstat_file:
                m = mdre.match(mdstat_line)
                if m:
                    arrays.append(m.group(1))
        return [cls(i) for i in arrays]
    
    def __detail(self):
        FNULL = open(os.devnull, 'w')
        result = subprocess.check_output(['/sbin/mdadm', '--detail', '/dev/md' + self.name], stderr=FNULL)
        return result.decode('utf-8')
    
    @property
    def status(self):
        """Returns array state: one of 'Optimal', 'Degraded', 'Critical', 'Reconstruction', 'Check', 'Unknown'"""
        status = parse_by_colon(self.__detail(), "State")
        return self.XLATE_STATUS.get(status, status)

    @property
    def stable_name(self):
        """Returns stable name for this array"""
        return parse_by_colon(self.__detail(), "Name").split(' ', 1)[0]

MD_RAID.register()

def zabbix_discover():
    print(json.dumps({'data': [{'{#RAIDINDEX}': str(i), '{#RAIDNAME}': i.stable_name} for i in RAID.discover()]}))
    
def zabbix_status(name):
    print(RAID.create(name).status)

def zabbix_stable_name(name):
    print(RAID.create(name).stable_name)

def main():
    parser = optparse.OptionParser(usage="usage: %prog [<options>] [<raidname>]")
    parser.add_option('--discover', default=False, action='store_true', dest='discover', help="discover RAID arrays as JSON for Zabbix autodiscover (raidname isn't used)")
    parser.add_option('--status', default=True, action="store_const", const="status", dest="action", help="return status of this array (default if no options given)")
    parser.add_option('--stable_name', default=False, action="store_const", const="stable_name", dest="action", help="returns stable name of this array as set in array metadata")
    (options, args) = parser.parse_args()
    if options.discover:
        zabbix_discover()
    elif len(args)==1:
        if options.action == "stable_name":
            zabbix_stable_name(args[0])
        else:
            zabbix_status(args[0])
    else:
        parser.print_help()

if __name__ == '__main__':
    main()
