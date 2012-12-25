# coding: utf-8
from os.path import join, basename
import os, sys, urllib, tarfile, setuptools, logging, stat, imp
import shutil
import ConfigParser
import distutils.core
from zc.buildout.easy_install import MissingDistribution
import zc.recipe.egg

import httplib
import rfc822
from urlparse import urlparse
from . import vcs
from . import utils

logger = logging.getLogger(__name__)

def rfc822_time(h):
    """Parse RFC 2822-formatted http header and return a time int."""
    rfc822.mktime_tz(rfc822.parsedate_tz(h))

main_software = object()

class BaseRecipe(object):
    """Base class for other recipes.

    It implements notably fetching of the main software part plus addons.
    The ``sources`` attributes is a dict storing how to fetch the main software
    part and specified addons. It has the following structure:

        local path -> (type, location_spec, options).

        where local path is the ``main_software`` object for the main software
        part, and otherwise a local path to an addons container.

        type can be
            - 'local'
            - 'downloadable'
            - one of the supported vcs

        location_spec is, depending on the type, a tuple specifying how to
        fetch : (url, None), or (vcs_url, vcs_revision) or None

        addons options are typically used to specify that the addons directory
        is actually a subdir of the specified one.

    """

    default_dl_url = { '6.0': 'http://www.openerp.com/download/stable/source/',
                       '6.1': 'http://nightly.openerp.com/6.1/releases/',
                       '7.0': 'http://nightly.openerp.com/7.0/releases/',
                       '5.0': 'http://v6.openerp.com/download/stable/source/',
                       }

    nightly_dl_url = {'6.1': 'http://nightly.openerp.com/6.1/nightly/src/',
                      '7.0': 'http://nightly.openerp.com/7.0/nightly/src/',
                      'trunk': 'http://nightly.openerp.com/trunk/nightly/src/',
                      }

    recipe_requirements = () # distribution required for the recipe itself
    recipe_requirements_paths = () # a default value is useful in unit tests
    requirements = () # requirements for what the recipe installs to run
    soft_requirements = () # subset of requirements that's not necessary

    # Caching logic for the main OpenERP part (e.g, without addons)
    # Can be 'filename' or 'http-head'
    main_http_caching = 'filename'

    def __init__(self, buildout, name, options):
        self.requirements = list(self.requirements)
        self.recipe_requirements_path = []
        self.buildout, self.name, self.options = buildout, name, options
        self.b_options = self.buildout['buildout']
        self.buildout_dir = self.b_options['directory']
        # GR: would prefer lower() but doing as in 'zc.recipe.egg'
        # (later) the standard way for all booleans is to use
        # options.query_bool() or get_bool(), but it doesn't lower() at all
        self.offline = self.b_options['offline'] == 'true'
        clear_locks = options.get('vcs-clear-locks', '').lower()
        self.vcs_clear_locks = clear_locks == 'true'
        clear_retry = options.get('vcs-clear-retry', '').lower()
        self.clear_retry = clear_retry == 'true'

        self.downloads_dir = self.make_absolute(
            self.b_options.get('openerp-downloads-directory', 'downloads'))
        self.version_wanted = None  # from the buildout
        self.version_detected = None  # string from the openerp setup.py
        self.parts = self.buildout['buildout']['parts-directory']
        self.sources = {}
        self.parse_addons(options)
        self.openerp_dir = None
        self.archive_filename = None
        self.archive_path = None # downloaded tar.gz

        if options.get('scripts') is None:
            options['scripts'] = ''

        # a dictionnary of messages to display in case a distribution is
        # not installable.
        self.missing_deps_instructions = {
            'PIL': ("You don't need to require it for OpenERP any more, since "
                    "the recipe automatically adds a dependency to Pillow. "
                    "If you really need it for other reasons, installing it "
                    "system-wide is a good option. "),
            }

        self.openerp_installed = []

        self.etc = self.make_absolute('etc')
        self.bin_dir = self.buildout['buildout']['bin-directory']
        self.config_path = join(self.etc, self.name + '.cfg')
        for d in self.downloads_dir, self.etc:
            if not os.path.exists(d):
                logger.info('Created %s/ directory' % basename(d))
                os.mkdir(d)

        self.parse_version()

    def parse_version(self):
        """Set the main software in ``sources`` and related attributes.
        """
        self.version_wanted = self.options.get('version')
        if self.version_wanted is None:
            raise ValueError('You must specify the version')

        self.preinstall_version_check()

        version_split = self.version_wanted.split()

        if len(version_split) == 1:
            # version can be a simple version name, such as 6.1-1
            major_wanted = self.version_wanted[:3]
            pattern = self.archive_filenames[major_wanted]
            if pattern is None:
                raise ValueError(
                    'OpenERP version %r is not supported' % self.version_wanted)

            self.archive_filename = pattern % self.version_wanted
            self.archive_path = join(self.downloads_dir, self.archive_filename)
            base_url = self.options.get(
                'base_url', self.default_dl_url[major_wanted])
            self.sources[main_software] = (
                'downloadable',
                ('/'.join((base_url.strip('/'), self.archive_filename)), None))
            return

        # in all other cases, the first token is the type of version
        type_spec = version_split[0]
        if type_spec in ('local', 'path'):
            self.openerp_dir = join(self.buildout_dir, version_split[1])
            self.sources[main_software] = ('local', None)
        elif type_spec == 'url':
            url = version_split[1]
            self.archive_filename = urlparse(url).path.split('/')[-1]
            self.archive_path = join(self.downloads_dir, self.archive_filename)
            self.sources[main_software] = ('downloadable', (url, None))
        elif type_spec == 'nightly':
            if len(version_split) != 3:
                raise ValueError(
                    "Unrecognized nightly version specification: "
                    "%r (expecting series, number) % version_split[1:]")
            self.nightly_series, self.version_wanted = version_split[1:]
            type_spec = 'downloadable'
            if self.version_wanted == 'latest':
                self.main_http_caching = 'http-head'
            series = self.nightly_series
            self.archive_filename = self.archive_nightly_filenames[series] % self.version_wanted
            self.archive_path = join(self.downloads_dir, self.archive_filename)
            base_url = self.options.get('base_url', self.nightly_dl_url[series])
            self.sources[main_software] = (
                'downloadable',
                ('/'.join((base_url.strip('/'), self.archive_filename)), None))
        else:
            # VCS types
            if len(version_split) != 4:
                raise ValueError("Unrecognized version specification: %r "
                                 "(expecting type, url, target, revision for "
                                 "remote repository or explicit download) " % (
                        version_split))

            type_spec, url, repo_dir, self.version_wanted = version_split
            self.openerp_dir = join(self.parts, repo_dir)
            self.sources[main_software] = (type_spec,
                                           (url, self.version_wanted))

    def preinstall_version_check(self):
        """Perform version checks before any attempt to install.

        To be subclassed.
        """

    def install_recipe_requirements(self):
        """Install requirements for the recipe to run."""
        to_install = self.recipe_requirements
        eggs_option = os.linesep.join(to_install)
        eggs = zc.recipe.egg.Eggs(self.buildout, '', dict(eggs=eggs_option))
        ws = eggs.install()
        _, ws = eggs.working_set()
        self.recipe_requirements_paths = [ws.by_key[dist].location
                                          for dist in to_install]
        sys.path.extend(self.recipe_requirements_paths)

    def merge_requirements(self):
        """Merge eggs option with self.requirements."""
        if 'eggs' not in self.options:
            self.options['eggs'] = '\n'.join(self.requirements)
        else:
            self.options['eggs'] += '\n' + '\n'.join(self.requirements)

    def install_requirements(self):
        """Install egg requirements and scripts.

        If some distributions are known as soft requirements, will retry
        without them
        """
        while True:
            eggs = zc.recipe.egg.Scripts(self.buildout, '', self.options)
            try:
                ws = eggs.install()
            except MissingDistribution, exc:
                project_name = exc.data[0].project_name
                msg = self.missing_deps_instructions.get(project_name)
                if msg is None:
                    raise
                logger.error("Could not find %r. " + msg, project_name)
                # GR this condition won't be enough in case of version
                # conditions in requirement
                if project_name not in self.soft_requirements:
                    sys.exit(1)
                else:
                    attempted = self.options['eggs'].split(os.linesep)
                    self.options['eggs'] = os.linesep.join(
                        [egg for egg in attempted if egg != project_name])
            else:
                break

        _, ws = eggs.working_set()
        self.ws = ws

    def apply_version_dependent_decisions(self):
        """Store some booleans depending on detected version.

        To be refined by subclasses.
        """
        pass

    @property
    def major_version(self):
        detected = self.version_detected
        if detected is None:
            return None
        return utils.major_version(detected)

    def read_openerp_setup(self):
        """Ugly method to extract requirements & version from ugly setup.py.

        Primarily designed for 6.0, but works with 6.1 as well.
        """
        old_setup = setuptools.setup
        old_distutils_setup = distutils.core.setup # 5.0 directly imports this
        def new_setup(*args, **kw):
            self.requirements.extend(kw.get('install_requires', ()))
            self.version_detected = kw['version']
        setuptools.setup = new_setup
        distutils.core.setup = new_setup
        sys.path.insert(0, '.')
        with open(join(self.openerp_dir,'setup.py'), 'rb') as f:
            saved_argv = sys.argv
            sys.argv = ['setup.py', 'develop']
            try:
                imp.load_module('setup', f, 'setup.py', ('.py', 'r', imp.PY_SOURCE))
            except SystemExit, exception:
                if 'dsextras' in exception.message:
                    raise EnvironmentError('Please first install PyGObject and PyGTK !')
                else:
                    raise EnvironmentError('Problem while reading OpenERP setup.py: ' + exception.message)
            except ImportError, exception:
                if 'babel' in exception.message:
                    raise EnvironmentError('OpenERP setup.py has an unwanted import Babel.\n'
                                           '=> First install Babel on your system or virtualenv :(\n'
                                           '(sudo aptitude install python-babel, or pip install babel)')
                else:
                    raise exception
            except Exception, exception:
                raise EnvironmentError('Problem while reading OpenERP setup.py: ' + exception.message)
            finally:
                sys.argv = saved_argv
        sys.path.pop(0)
        setuptools.setup = old_setup
        distutils.core.setup = old_distutils_setup
        self.apply_version_dependent_decisions()

    def make_absolute(self, path):
        """Make a path absolute if needed.

        If not already absolute, it is interpreted as relative to the
        buildout directory."""
        if os.path.isabs(path):
            return path
        return join(self.buildout_dir, path)

    def sandboxed_tar_extract(self, sandbox, tarfile, first=None):
        """Extract those members that are below the tarfile path 'sandbox'.

        The tarfile module official doc warns against attacks with .. in tar.

        The option to start with a first member is useful for this case, since
        the recipe consumes a first member in the tar file to get the openerp
        main directory in parts.
        It is taken for granted that this first member has already been checked.
        """

        if first is not None:
            tarfile.extract(first)

        for tinfo in tarfile:
            if tinfo.name.startswith(sandbox + '/'):
                tarfile.extract(tinfo)
            else:
                logger.warn('Tarball member %r is outside of %r. Ignored.',
                            tinfo, sandbox)

    def _produce_setup_without_pil(self, src_directory):
        """Create a copy of setup.py without PIL and return a path to it."""

        new_setup_path = join(src_directory, 'setup.nopil.py')
        with open(join(src_directory, 'setup.py')) as inp:
            setup_str = inp.read()
        with open(new_setup_path, 'w') as out:
            out.write(setup_str.replace("'PIL',", ''))
        return new_setup_path

    def develop(self, src_directory, setup_has_pil=False):
        """Develop the specified source distribution.

        Any call to zc.recipe.eggs will use that developped version.
        develop() launches a subprocess, to which we need to forward
        the paths to requirements via PYTHONPATH.
        If setup_has_pil is True, an altered version of setup that does not
        require it is produced to perform the develop.
        """
        logger.debug("Developing %r", src_directory)
        develop_dir = self.b_options['develop-eggs-directory']
        pythonpath_bak = os.getenv('PYTHONPATH')
        os.putenv('PYTHONPATH', ':'.join(self.recipe_requirements_paths))

        if setup_has_pil:
            setup = self._produce_setup_without_pil(src_directory)
        else:
            setup = src_directory

        try:
            zc.buildout.easy_install.develop(setup, develop_dir)
        finally:
            if setup_has_pil:
                os.unlink(setup)

        if pythonpath_bak is None:
            os.unsetenv('PYTHONPATH')
        else:
            os.putenv('PYTHONPATH', pythonpath_bak)

    def parse_addons(self, options):
        """Parse the addons options into the ``addons`` attribute.

        See ``BaseRecipe`` docstring for details about the ``addons`` dict.
        """

        for line in options.get('addons', '').split(os.linesep):
            split = line.split()
            if not split:
                return
            loc_type = split[0]
            spec_len = 2 if loc_type == 'local' else 4

            options = dict(opt.split('=') for opt in split[spec_len:])
            if loc_type == 'local':
                addons_dir = split[1]
                location_spec = None
            else: # vcs
                repo_url, addons_dir, repo_rev = split[1:4]
                location_spec = (repo_url, repo_rev)

            self.sources[addons_dir] = (loc_type, location_spec, options)

    def retrieve_addons(self):
        """Parse the addons option line, download and return a list of paths.

        syntax: repo_type repo_url repo_dir repo_rev [options]
              or an absolute or relative path
        options are themselves in the key=value form
        """
        sources = self.sources.items()
        if not sources:
            return []

        addons_paths = []

        for local_dir, source_spec in sources:
            if local_dir is main_software:
                continue

            loc_type, loc_spec, addons_options = source_spec
            local_dir = self.make_absolute(local_dir)
            options = dict(offline=self.offline,
                           clear_locks=self.vcs_clear_locks)

            if loc_type != 'local':
                for k, v in self.options.items():
                    if k.startswith(loc_type + '-'):
                        options[k] = v

                repo_url, repo_rev = loc_spec
                vcs.get_update(loc_type, local_dir, repo_url, repo_rev,
                               clear_retry=self.clear_retry,
                               **options)

            subdir = addons_options.get('subdir')
            addons_dir = join(local_dir, subdir) if subdir else local_dir

            manifest = os.path.join(addons_dir, '__openerp__.py')
            if os.path.isfile(manifest):
                # repo is a single addon, put it actually below
                name = os.path.split(addons_dir)[1]
                c = 0
                tmp = addons_dir + '_%d' % c
                while os.path.exists(tmp):
                    c += 1
                    tmp = addons_dir + '_%d' % c
                os.rename(addons_dir, tmp)
                os.mkdir(addons_dir)
                new_dir = join(addons_dir, name)
                os.rename(tmp, new_dir)
            addons_paths.append(addons_dir)
        return addons_paths

    def main_download(self):
        """HTTP download for main part of the software to self.archive_path.
        """
        if self.offline:
            raise IOError("%s not found, and offline mode requested" % self.archive_path)
        url = self.sources[main_software][1][0]
        logger.info("Downloading %s ..." % url)

        try:
            msg = urllib.urlretrieve(url, self.archive_path)
            if msg[1].type == 'text/html':
                os.unlink(self.archive_path)
                raise LookupError(
                    'Wanted version %r not found on server (tried %s)' % (
                        self.version_wanted, url))

        except (tarfile.TarError, IOError):
            # GR: ContentTooShortError subclasses IOError
            os.unlink(self.archive_path)
            raise IOError('The archive does not seem valid: ' +
                          repr(self.archive_path))

    def is_stale_http_head(self):
        """Tell if the download is stale by doing a HEAD request.

        Assumes the correct date had been written upon download.
        This is the same system as in GNU Wget 1.12. It works even if
        the server does not implement conditional responses such as 304
        """
        archivestat = os.stat(self.archive_path)
        length, modified = archivestat.st_size, archivestat.st_mtime

        url = self.sources[main_software][1][0]
        logger.info("Checking if %s if fresh wrt %s",
                    self.archive_path, url)
        parsed = urlparse(url)
        if parsed.scheme == 'https':
            cnx_cls = httplib.HTTPSConnection
        else:
            cnx_cls = httplib.HTTPConnection
        try:
            cnx = cnx_cls(parsed.netloc)
            cnx.request('HEAD', parsed.path) # TODO query ? fragment ?
            res = cnx.getresponse()
        except IOError:
            return True

        if res.status != 200:
            return True

        if int(res.getheader('Content-Length')) != length:
            return True

        head_modified = res.getheader('Last-Modified')
        logger.debug("Last-modified from HEAD request: %s", head_modified)
        if rfc822_time(head_modified) > modified:
            return True

        logger.info("No need to re-download %s", self.archive_path)

    def install(self):
        os.chdir(self.parts)

        freeze_to = self.options.get('freeze-to')
        if freeze_to is not None and not self.offline:
            raise ValueError("To freeze a part, you must run offline "
                             "so that there's no modification from what "
                             "you just tested. Please rerun with -o.")

        # install server, webclient or gtkclient
        source = self.sources[main_software]
        type_spec = source[0]
        logger.info('Selected install type: %s', type_spec)
        if type_spec == 'local':
            logger.info('Local directory chosen, nothing to do')
        elif type_spec == 'downloadable':
            # download if needed
            if ((self.archive_path  and not os.path.exists(self.archive_path))
                 or (self.main_http_caching == 'http-head'
                     and self.is_stale_http_head())):
                self.main_download()

            logger.info(u'Inspecting %s ...' % self.archive_path)
            tar = tarfile.open(self.archive_path)
            first = tar.next()
            # Everything that follows assumes all tarball members
            # are inside a directory with an expected name such
            # as openerp-6.1-1
            assert(first.isdir())
            extracted_name = first.name.split('/')[0]
            self.openerp_dir = join(self.parts, extracted_name)
            # protection against malicious tarballs
            assert(not os.path.isabs(extracted_name))
            assert(self.openerp_dir.startswith(self.parts))

            logger.info("Cleaning existing %s", self.openerp_dir)
            if os.path.exists(self.openerp_dir):
                shutil.rmtree(self.openerp_dir)
            logger.info(u'Extracting %s ...' % self.archive_path)
            self.sandboxed_tar_extract(extracted_name, tar, first=first)
            tar.close()
        else:
            url, rev = source[1]
            vcs.get_update(type_spec, self.openerp_dir, url, rev,
                           offline=self.offline, clear_retry=self.clear_retry)

        addons_paths = self.retrieve_addons()
        for path in addons_paths:
            assert os.path.isdir(path), (
                "Not a directory: %r (aborting)" % path)

        self.install_recipe_requirements()
        os.chdir(self.openerp_dir) # GR probably not needed any more
        self.read_openerp_setup()
        if type_spec == 'downloadable' and self.version_wanted == 'latest':
            self.nightly_version = self.version_detected.split('-', 1)[1]
            logger.warn("Detected 'nighlty latest version', you may want to "
                        "fix it in your config file for replayability: \n    "
                        "version = " + self.dump_nightly_latest_version())
        is_60 = self.major_version == (6, 0)
        # configure addons_path option
        if addons_paths:
            if 'options.addons_path' not in self.options:
                self.options['options.addons_path'] = ''
            if is_60:
                self.options['options.addons_path'] += join(self.openerp_dir, 'bin', 'addons') + ','
            else:
                self.options['options.addons_path'] += join(self.openerp_dir, 'openerp', 'addons') + ','

            self.options['options.addons_path'] += ','.join(addons_paths)
        elif is_60:
            self._60_default_addons_path()

        if is_60:
            self._60_fix_root_path()

        # add openerp paths into the extra-paths
        if self.major_version >= (6, 2):
            paths = [self.openerp_dir,
                     join(self.openerp_dir, 'addons')] # TODO necessary ?
        else:
            paths = [join(self.openerp_dir, 'bin'),
                     join(self.openerp_dir, 'bin', 'addons')]
        paths.append(self.options.get('extra-paths', ''))
        self.options['extra-paths'] = os.linesep.join(paths)

        if self.version_detected is None:
            raise EnvironmentError('Version of OpenERP could not be detected')
        self.merge_requirements()
        self.install_requirements()

        self._install_startup_scripts()

        # create the config file
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        logger.info('Creating config file: ' + join(basename(self.etc), basename(self.config_path)))
        self._create_default_config()

        # modify the config file according to recipe options
        config = ConfigParser.RawConfigParser()
        config.read(self.config_path)
        for recipe_option in self.options:
            if '.' not in recipe_option:
                continue
            section, option = recipe_option.split('.', 1)
            if not config.has_section(section):
                config.add_section(section)
            config.set(section, option, self.options[recipe_option])
        with open(self.config_path, 'wb') as configfile:
            config.write(configfile)

        if freeze_to:
            self.freeze_to(freeze_to)
        return self.openerp_installed

    def dump_nightly_latest_version(self):
        """After download/analysis of 'nightly latest', give equivalent spec.
        """
        return ' '.join((self.nightly_series, 'nightly', self.nightly_version))

    def freeze_to(self, out_config_path):
        """Create an extension buildout freezing current revisions & versions.
        """

        out_conf = ConfigParser.ConfigParser()

        frozen = getattr(self.buildout, '_openerp_recipe_frozen', None)
        if frozen is None:
            frozen = self.buildout._openerp_recipe_frozen = set()

        if out_config_path in frozen:
            # read configuration started by other recipe
            out_conf.read(self.make_absolute(out_config_path))
        else:
            out_conf.add_section('buildout')
            out_conf.set('buildout', 'extends', self.buildout_cfg_name())

        out_conf.add_section(self.name)
        addons_option = []
        for local_path, location_spec in self.sources.items():
            type_spec = location_spec[0]
            if type_spec == 'local':
                continue

            if local_path is main_software:
                # don't dump the resolved URL, as future reproduction may be
                # better done with another URL base holding archived old
                # versions : it's better to let tomorrow logic handle that
                # from high level information.
                if self.version_wanted == 'latest':
                    out_conf.set(self.name, 'version',
                                 self.dump_nightly_latest_version())
                    continue
                abspath = self.openerp_dir
                self.cleanup_openerp_dir()
            else:
                abspath = vcs.HgRepo.fix_target(self.make_absolute(local_path))

            url, rev = location_spec[1]
            repo = vcs.SUPPORTED[type_spec](abspath, url)

            if repo.uncommitted_changes():
                raise RuntimeError("You have uncommitted changes or "
                                   "non ignored untracked files in %r. "
                                   "Unsafe to freeze. Please commit or "
                                   "revert and test again !" % abspath)

            parents = repo.parents()
            if len(parents) > 1:
                raise RuntimeError("Current context of %r has several "
                                   "parents. Ongoing merge ? "
                                   "Can't freeze." % abspath)

            revision = parents[0]
            if local_path is main_software:
                addons_option.insert(0, '%s  ; main software part' % revision)
            else:
                addons_option.append(' '.join((local_path, revision)))

        if addons_option:
            out_conf.set(self.name, 'revisions', os.linesep.join(addons_option))

        with open(self.make_absolute(out_config_path), 'w') as out:
            out_conf.write(out)
        frozen.add(out_config_path)

    def _install_script(self, name, content):
        """Install and register a script with prescribed name and content.

        Return the script path
        """
        path = join(self.bin_dir, name)
        f = open(path, 'w')
        f.write(content)
        f.close()
        os.chmod(path, stat.S_IRWXU)
        self.openerp_installed.append(path)
        return path

    def _install_startup_scripts(self):
        raise NotImplementedError

    def _create_default_config(self):
        raise NotImplementedError

    update = install

    def _60_fix_root_path(self):
        """Correction of root path for OpenERP 6.0 pure python install

        Actual implementation is up to subclasses
        """

    def _60_default_addons_path(self):
        """Set the default addons patth for OpenERP 6.0 pure python install

        Actual implementation is up to subclasses
        """

    def cleanup_openerp_dir(self):
        """Revert local modifications that have been made during installation.

        These can be, e.g., forbidden by the freeze process."""

        shutil.rmtree(join(self.openerp_dir, 'openerp.egg-info'))
        # setup rewritten without PIL is cleaned during the process itself

    def buildout_cfg_name(self, argv=None):
        """Return the name of the config file that's been called.
        """

        # not using optparse because it's not obvious how to tell it to
        # consider just one option and ignore the others.

        if argv is None:
            argv = sys.argv[1:]

        # -c FILE or --config FILE syntax
        for opt in ('-c', '--config'):
            try:
                i = argv.index(opt)
            except ValueError:
                continue
            else:
                return argv[i+1]

        # --config=FILE syntax
        prefix="--config="
        for a in argv:
            if a.startswith(prefix):
                return a[len(prefix):]

        return 'buildout.cfg'

