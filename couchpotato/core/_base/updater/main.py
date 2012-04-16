from couchpotato.api import addApiView
from couchpotato.core.event import addEvent, fireEvent, fireEventAsync
from couchpotato.core.helpers.request import jsonified
from couchpotato.core.logger import CPLog
from couchpotato.core.plugins.base import Plugin
from couchpotato.environment import Env
from datetime import datetime
from git.repository import LocalRepository
from pip.index import PackageFinder
from pip.locations import build_prefix, src_prefix
from pip.req import InstallRequirement, RequirementSet
import os
import time
import traceback

log = CPLog(__name__)


class CPRepo(LocalRepository):

    _git_command = None

    def __init__(self, path, command = 'git'):
        self.setCommand(command)
        super(CPRepo, self).__init__(path)

    def setCommand(self, command):
        self._git_command = command

    def _executeGitCommand(self, command, cwd = None):
        command = '%s %s' % (self._git_command, str(command.replace('git ', '')))

        return super(CPRepo, self)._executeGitCommand(command, cwd)


class Updater(Plugin):

    app_dir = None
    repo_name = 'RuudBurger/CouchPotatoServer'

    version = None
    update_failed = False
    update_version = None
    last_check = 0

    def __init__(self):

        self.app_dir = Env.get('app_dir')
        self.repo = CPRepo(self.app_dir, command = self.conf('git_command', default = 'git'))

        def test():
            self.updateRequirements()
            #subprocess.call(['pip', 'install', '-r', os.path.join(self.app_dir, 'requirements.txt')])
        addEvent('app.load', test)

        fireEvent('schedule.interval', 'updater.check', self.check, hours = 6)

        addEvent('app.load', self.check)

        addApiView('updater.info', self.getInfo, docs = {
            'desc': 'Get updater information',
            'return': {
                'type': 'object',
                'example': """{
        'repo_name': "Name of used repository",
        'last_check': "last checked for update",
        'update_version': "available update version or empty",
        'version': current_cp_version
}"""}
        })
        addApiView('updater.update', self.doUpdateView)
        addApiView('updater.check', self.checkView, docs = {
            'desc': 'Check for available update',
            'return': {'type': 'see updater.info'}
        })

    def updateRequirements(self):
        requirement_set = RequirementSet(build_dir = build_prefix, src_dir = src_prefix, download_dir = None)

        f = open(os.path.join(self.app_dir, 'requirements.txt'))
        lines = f.readlines()
        f.close()

        for line in lines:
            requirement_set.add_requirement(InstallRequirement.from_line(line.strip(), None))

        finder = PackageFinder(find_links = [], index_urls = ["http://pypi.python.org/simple/"])

        install_options = []
        global_options = []
        requirement_set.prepare_files(finder, force_root_egg_info = False, bundle = False)
        requirement_set.install(install_options, global_options)

        for requirement in requirement_set.requirements.values():
            print requirement.install_succeeded or requirement.satisfied_by


    def getInfo(self):

        return jsonified(self.info())

    def info(self):
        return {
            'repo_name': self.repo_name,
            'last_check': self.last_check,
            'update_version': self.update_version,
            'version': self.getVersion()
        }

    def getVersion(self):

        if not self.version:
            try:
                output = self.repo.getHead() # Yes, please
                log.debug('Git version output: %s' % output.hash)
                self.version = {
                    'hash': output.hash[:8],
                    'date': output.getDate(),
                }
            except Exception, e:
                log.error('Failed using GIT updater, running from source, you need to have GIT installed. %s' % e)
                return 'No GIT'

        return self.version

    def check(self):

        if self.update_version or self.isDisabled():
            return

        log.info('Checking for new version on github for %s' % self.repo_name)
        if not Env.get('dev'):
            self.repo.fetch()

        current_branch = self.repo.getCurrentBranch().name

        for branch in self.repo.getRemoteByName('origin').getBranches():
            if current_branch == branch.name:

                local = self.repo.getHead()
                remote = branch.getHead()

                log.info('Versions, local:%s, remote:%s' % (local.hash[:8], remote.hash[:8]))

                if local.getDate() < remote.getDate():
                    self.update_version = {
                        'hash': remote.hash[:8],
                        'date': remote.getDate(),
                    }
                    if self.conf('automatic') and not self.update_failed:
                        if self.doUpdate():
                            fireEventAsync('app.crappy_restart')
                    else:
                        if self.conf('notification'):
                            fireEvent('updater.available', message = 'A new update is available', data = self.getVersion())

        self.last_check = time.time()

    def checkView(self):
        self.check()
        return self.getInfo()

    def doUpdateView(self):
        return jsonified({
            'success': self.doUpdate()
        })

    def doUpdate(self):
        try:
            log.debug('Stashing local changes')
            self.repo.saveStash()

            log.info('Updating to latest version')
            info = self.info()
            self.repo.pull()

            # Delete leftover .pyc files
            self.deletePyc()

            # Update libs
            #bin = 'Scripts' if sys.platform == 'win32' else 'bin'
            #subprocess.call([os.path.join(home_dir, bin, 'pip'),'install', '-r', os.path.join(self.app_dir, 'requirements.txt')])

            # Notify before returning and restarting
            version_date = datetime.fromtimestamp(info['update_version']['date'])
            fireEvent('updater.updated', 'Updated to a new version with hash "%s", this version is from %s' % (info['update_version']['hash'], version_date), data = info)

            return True
        except:
            log.error('Failed updating via GIT: %s' % traceback.format_exc())

        self.update_failed = True

        return False

    def deletePyc(self):

        for root, dirs, files in os.walk(Env.get('app_dir')):

            pyc_files = filter(lambda filename: filename.endswith('.pyc'), files)
            py_files = set(filter(lambda filename: filename.endswith('.py'), files))
            excess_pyc_files = filter(lambda pyc_filename: pyc_filename[:-1] not in py_files, pyc_files)

            for excess_pyc_file in excess_pyc_files:
                full_path = os.path.join(root, excess_pyc_file)
                log.debug('Removing old PYC file: %s' % full_path)
                try:
                    os.remove(full_path)
                except:
                    log.error('Couldn\'t remove %s: %s' % (full_path, traceback.format_exc()))

            for dir_name in dirs:
                full_path = os.path.join(root, dir_name)
                if len(os.listdir(full_path)) == 0:
                    try:
                        os.rmdir(full_path)
                    except:
                        log.error('Couldn\'t remove empty directory %s: %s' % (full_path, traceback.format_exc()))

    def isEnabled(self):
        return super(Updater, self).isEnabled() and Env.get('uses_git')
