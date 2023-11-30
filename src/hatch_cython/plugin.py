import os
import re
import subprocess
import sys
from contextlib import contextmanager
from glob import glob
from tempfile import TemporaryDirectory
from typing import ClassVar

from Cython.Tempita import sub as render_template
from hatchling.builders.hooks.plugin.interface import BuildHookInterface

from hatch_cython.config import parse_from_dict
from hatch_cython.temp import ExtensionArg, setup_py
from hatch_cython.types import CallableT, DictT, ListStr, ListT, P
from hatch_cython.utils import autogenerated, memo, parse_user_glob, plat


class CythonBuildHook(BuildHookInterface):
    PLUGIN_NAME = "cython"

    precompiled_extensions: ClassVar[list] = [
        # py is left out as we have it optional / runtime value
        ".pyx",
        ".pxd",
    ]
    templated_extensions: ClassVar[list] = [f"{f}.in" for f in {".py", ".pyi", *precompiled_extensions}]
    intermediate_extensions: ClassVar[list] = [
        ".c",
        ".cpp",
    ]
    compiled_extensions: ClassVar[list] = [
        ".dll",
        # unix
        ".so",
        # windows
        ".pyd",
    ]

    def __init__(self, *args: P.args, **kwargs: P.kwargs):
        super().__init__(*args, **kwargs)

    @property
    @memo
    def is_src(self):
        return os.path.exists(os.path.join(self.root, "src"))

    @property
    def is_windows(self):
        return plat() == "windows"

    def normalize_path(self, pattern: str):
        if self.is_windows:
            return pattern.replace("/", "\\")
        return pattern.replace("\\", "/")

    def normalize_glob(self, pattern: str):
        return pattern.replace("\\", "/")

    @property
    @memo
    def dir_name(self):
        return self.options.src if self.options.src is not None else self.metadata.name

    @property
    @memo
    def project_dir(self):
        if self.is_src:
            src = f"./src/{self.dir_name}"
        else:
            src = f"./{self.dir_name}"
        return src

    def render_templates(self):
        for template in self.templated_globs:
            outfile = template[:-3]
            with open(template, encoding="utf-8") as f:
                tmpl = f.read()

            kwds = self.options.templates.find(self, outfile, template)
            data = render_template(tmpl, **kwds)
            with open(outfile, "w", encoding="utf-8") as f:
                f.write(autogenerated(kwds) + "\n\n" + data)

    @property
    @memo
    def precompiled_globs(self):
        _globs = []
        for ex in self.precompiled_extensions:
            _globs.extend((f"{self.project_dir}/*{ex}", f"{self.project_dir}/**/*{ex}"))
        return list(set(_globs))

    @property
    @memo
    def options_exclude(self):
        return [parse_user_glob(e.matches) for e in self.options.files.exclude if e.applies()]

    def wanted(self, item: str):
        return not any(re.match(e, self.normalize_glob(item), re.IGNORECASE) for e in self.options_exclude)

    def filter_ensure_wanted(self, tgts: ListStr):
        return list(
            filter(
                self.wanted,
                tgts,
            )
        )

    @property
    def included_files(self):
        included = []
        _normu = [self.normalize_glob(parse_user_glob(e.matches)) for e in self.options.files.exclude if e.applies()]
        self.app.display_debug("user globs")
        self.app.display_debug(_normu)
        for patt in self.precompiled_globs:
            globbed = glob(patt, recursive=True)
            if len(globbed) == 0:
                continue
            matched = self.filter_ensure_wanted(globbed)
            included.extend(matched)
        return included

    @property
    def normalized_included_files(self):
        """
        Produces files in posix format
        """
        return list({self.normalize_glob(f) for f in self.included_files})

    def normalize_aliased_filelike(self, path: str):
        # sometimes we end up with a case where non src produces
        # '..example_lib._alias'
        while ".." in path:
            path = path.replace("..", "")
        return path

    @property
    def grouped_included_files(self) -> ListT[ExtensionArg]:
        grouped: DictT[str, set] = {}
        for norm in self.normalized_included_files:
            root, ext = os.path.splitext(norm)
            ok = True
            if ext == ".pxd":
                pyfile = norm.replace(".pxd", ".py")
                if os.path.exists(pyfile):
                    norm = pyfile  # noqa: PLW2901
                else:
                    ok = False
                    self.app.display_warning(f"attempted to use .pxd file without .py file ({norm})")
            if self.is_src:
                root = root.replace("./src/", "")
            root = self.normalize_aliased_filelike(root.replace("/", "."))
            alias = self.options.files.matches_alias(root)
            self.app.display_debug(f"check alias {ok} {root} -> {norm} -> {alias}")
            if alias:
                root = alias
                self.app.display_debug(f"aliased {root} -> {norm}")
            if grouped.get(root) and ok:
                grouped[root].add(norm)
            elif ok:
                grouped[root] = {norm}
        return [ExtensionArg(name=key, files=list(files)) for key, files in grouped.items()]

    @property
    @memo
    def artifact_globs(self):
        artifact_globs = []
        for included_file in self.normalized_included_files:
            root, _ = os.path.splitext(included_file)
            artifact_globs.extend(f"{root}.*{ext}" for ext in self.precompiled_extensions)
        return artifact_globs

    @property
    @memo
    def templated_globs(self):
        return self._globs(self.templated_extensions)

    @property
    @memo
    def normalized_dist_globs(self):
        return list(map(self.normalize_glob, self.artifact_globs))

    def artifact_patterns(self, source: ListStr):
        return [f"/{artifact_glob}" for artifact_glob in source]

    @property
    def artifacts(self):
        return (
            self.artifact_patterns(self.normalized_dist_globs)
            if not self.sdist
            else self.artifact_patterns(self.intermediate)
        )

    @contextmanager
    def get_build_dirs(self):
        with TemporaryDirectory() as temp_dir:
            yield os.path.realpath(temp_dir)

    def _globs(self, exts: ListStr, normalize: CallableT[[str], str] = None):
        if normalize is None:
            normalize = self.normalize_glob
        globs = [
            *(f"{self.project_dir}/**/*{ext}" for ext in exts),
            *(f"{self.project_dir}/*{ext}" for ext in exts),
        ]
        globbed = []
        for g in globs:
            globbed += list(map(normalize, glob(g, recursive=True)))
        return list(filter(self.wanted, set(globbed)))

    @property
    def precompiled(self):
        return self._globs(self.precompiled_extensions, self.normalize_glob)

    @property
    def intermediate(self):
        return self._globs(self.intermediate_extensions, self.normalize_path)

    @property
    def compiled(self):
        return self._globs(self.compiled_extensions, self.normalize_path)

    @property
    def autogenerated(self):
        return list(filter(os.path.exists, (s.replace(".in", "") for s in self.templated_globs)))

    @property
    def inclusion_map(self):
        include = {}
        for compl in self.compiled:
            include[compl] = compl
        self.app.display_debug("Derived inclusion map")
        self.app.display_debug(include)
        return include

    def rm_recurse(self, li: ListStr):
        self.app.display_debug("Removing by match")
        self.app.display_debug(li)
        for f in li:
            os.remove(f)

    def clean(self, _: ListStr):
        self.rm_recurse(self.autogenerated)
        self.rm_recurse(self.intermediate)
        self.rm_recurse(self.compiled)

    @property
    @memo
    def options(self):
        config = parse_from_dict(self)
        if config.compile_py:
            self.precompiled_extensions.append(".py")
        return config

    @property
    def sdist(self):
        return self.target_name == "sdist"

    @property
    def wheel(self):
        return self.target_name == "wheel"

    def build_ext(self):
        with self.get_build_dirs() as temp:
            self.render_templates()

            shared_temp_build_dir = os.path.join(temp, "build")
            temp_build_dir = os.path.join(temp, "tmp")

            os.mkdir(shared_temp_build_dir)
            os.mkdir(temp_build_dir)

            self.app.display_info("Building c/c++ extensions...")
            self.app.display_info(self.normalized_included_files)
            setup_file = os.path.join(temp, "setup.py")
            with open(setup_file, "w") as f:
                setup = setup_py(
                    *self.grouped_included_files,
                    options=self.options,
                    sdist=self.sdist,
                )
                self.app.display_debug(setup)
                f.write(setup)

            self.options.validate_include_opts()

            process = subprocess.run(  # noqa: PLW1510
                [  # noqa: S603
                    sys.executable,
                    setup_file,
                    "build_ext",
                    "--inplace",
                    "--verbose",
                    "--build-lib",
                    shared_temp_build_dir,
                    "--build-temp",
                    temp_build_dir,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.options.envflags.env,
            )
            stdout = process.stdout.decode("utf-8")
            if process.returncode:
                self.app.display_error(f"cythonize exited non null status {process.returncode}")
                self.app.display_error(stdout)
                msg = "failed compilation"
                raise Exception(msg)
            else:
                self.app.display_info(stdout)

            self.app.display_success("Post-build artifacts")

    def initialize(self, _: str, build_data: dict):
        self.app.display_mini_header(self.PLUGIN_NAME)
        self.app.display_debug("options")
        self.app.display_debug(self.options.asdict(), level=1)
        self.app.display_debug("sdist")
        self.app.display_debug(self.sdist, level=1)
        self.app.display_waiting("pre-build artifacts")

        if len(self.grouped_included_files) != 0:
            self.build_ext()
            self.app.display_info(glob(f"{self.project_dir}/*/**", recursive=True))

        if self.sdist and not self.options.compiled_sdist:
            self.clean(None)

        build_data["infer_tag"] = True
        build_data["artifacts"].extend(self.artifacts)
        build_data["force_include"].update(self.inclusion_map)
        build_data["pure_python"] = False

        self.app.display_info("Extensions complete")
        self.app.display_debug(build_data)
