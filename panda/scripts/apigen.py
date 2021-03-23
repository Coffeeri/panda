#!/usr/bin/env python3

import os
import re
import sys
import textwrap
import pycparser
import subprocess

KNOWN_TYPES = ['bool', 'int', 'double', 'float', 'char', 'short', 'long', 'uint8_t', 'uint16_t', 'uint32_t', 'uint64_t']
PROTOTYPE_RE = re.compile('^([^(]+)\((.*)\)\s*\;')

# input is name of interface file.
# output is list of args for that fn.
# so, for the fn
# void taint2_labelset_llvm_iter(int reg_num, int offset, int (*app)(uint32_t el, void *stuff1), void *stuff2);
# this will return
# ["reg_num", "offset", "app", "stuff2"]
#
def get_arglists(pf):
    pyc = pycparser.CParser()
    p = pyc.parse(pf)
    args = {}
    for (dc, d) in p.children():
        if type(d) == pycparser.c_ast.Decl:
            # a prototype
            function_name = d.name
            #print "function name = [%s]" % function_name
            fundec = d.children()[0][1]
            args[function_name] = []
            for arg in fundec.args.params:
                if not (arg.name is None):
                    args[function_name].append(arg.name)
    return args


# prototype_line is a string containint a c function prototype.
# all on one line.  has to end with a semi-colon.
# return type has to be simple (better not return a fn ptr).
# it can return a pointer to something.
# this fn splits that line up into 
# return_type,
# fn name
# fn args (with types)
def split_fun_prototype(prototype_line):
    foo = PROTOTYPE_RE.search(prototype_line)
    if foo is None:
        return None
    (a, fn_args_with_types) = foo.groups()
    bar = a.split()
    fn_name = bar[-1]
    fn_type = " ".join(bar[0:-1])
    # carve off ptrs from head of fn name
    while fn_name[0] == '*':
        fn_name = fn_name[1:]
        fn_type = fn_type + " *"
    return (fn_type, fn_name, fn_args_with_types)


def generate_code(functions, module, includes):
    code = textwrap.dedent("""\
        #ifndef __{0}_EXT_H__
        #define __{0}_EXT_H__
        /*
         * DO NOT MODIFY. This file is automatically generated by scripts/apigen.py,
         * based on the <plugin>_int.h file in your plugin directory.
         *
         * Note: Function pointers for API calls are declared as extern.
         * The definition of the pointers is guarded by the PLUGIN_MAIN macro.
         * This plugin is defined only for the compilation unit matching the
         * name of the plugin.
         * This allows us to initialize API function pointers once, in the main
         * compilation unit, rather than in every compilation unit.
         */
        #include <dlfcn.h>
        #include "panda/plugin_api.h"
    """).format(module.upper())

    # convert function specs to maps
    fn_keys = ['rtype', 'name', 'args_with_types', 'args_list']
    functions = [dict(list(zip(fn_keys, fn_spec))) for fn_spec in functions]

    for fn in functions:
        fn['args'] = ','.join(fn['args_list'])

        code += textwrap.dedent("""
            typedef {rtype}(*{name}_t)({args_with_types});
            extern {name}_t __{name};
            #ifdef PLUGIN_MAIN
            {name}_t __{name} = NULL;
            #endif
            static inline {rtype} {name}({args_with_types});
            static inline {rtype} {name}({args_with_types}) {{
                assert(__{name});
                return __{name}({args});
            }}
        """).format(**fn)

    ppp_imports = "\n    ".join(['IMPORT_PPP(module, {name})'.format(**fn) for fn in functions])

    code += textwrap.dedent("""
        bool init_{0}_api(void);

        #ifdef PLUGIN_MAIN
        #define API_PLUGIN_NAME "{0}"
        #define IMPORT_PPP(module, func_name) {{ \\
            __##func_name = (func_name##_t) dlsym(module, #func_name); \\
            char *err = dlerror(); \\
            if (err) {{ \\
                printf("Couldn't find %s function in library %s.\\n", #func_name, API_PLUGIN_NAME); \\
                printf("Error: %s\\n", err); \\
                return false; \\
            }} \\
        }}
        bool init_{0}_api(void) {{
            void *module = panda_get_plugin_by_name(API_PLUGIN_NAME);
            if (!module) {{
                fprintf(stderr, "Couldn't load %s plugin: %s\\n", API_PLUGIN_NAME, dlerror());
                return false;
            }}
            {1}
            return true;
        }}
        #undef API_PLUGIN_NAME
        #undef IMPORT_PPP
        #endif

        #endif
    """).format(module, ppp_imports)

    return code

bad_keywords = ['static', 'inline']
keep_keywords = ['const', 'unsigned']
def resolve_type(modifiers, name):
    modifiers = modifiers.strip()
    tokens = modifiers.split()
    if len(tokens) > 1:
        # we have to go through all the keywords we care about
        relevant = []
        for token in tokens[:-1]:
            if token in keep_keywords:
                relevant.append(token)
            if token in bad_keywords:
                raise Exception("Invalid token in API function definition")
        relevant.append(tokens[-1])
        rtype = " ".join(relevant)
    else:
        rtype = tokens[0]
    if name.startswith('*'):
        return rtype+'*', name[1:]
    else:
        return rtype, name

def generate_api(interface_file, ext_file, extra_gcc_args):
    functions = []
    includes = []

    # use preprocessor
    pf = subprocess.check_output(['gcc', '-E', interface_file] + extra_gcc_args).decode()

    # use pycparser to get arglists
    arglist = get_arglists(pf)

    for line in pf.split("\n"):
        line = line.strip();
        if not line or line.startswith('#'):    # empty line or preprocessor directive
            continue

        # attempt to parse as function prototype
        # if successful, a tuple of (rtype, fn_name, args_with_types) is returned
        func_spec = split_fun_prototype(line)

        if func_spec is None:       # not a function prototype
            continue
        else:                       # append argument names func_spec tuple
            func_spec += (arglist[func_spec[1]],)

        functions.append(func_spec)

    # Plugin interface file will look like [...]/plugins/<name>/<name>_int.h
    plugin_name = os.path.basename(os.path.dirname(interface_file))
    code = generate_code(functions, plugin_name, includes)
    with open(ext_file,"w") as extAPI:
        extAPI.write(code)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.stderr.write("usage: %s <interface_file.h> <external_api_file.h> extra gcc args" % sys.argv[0])
        sys.exit(1)
    generate_api(sys.argv[1], sys.argv[2], sys.argv[3:])

# vim: set tabstop=4 softtabstop=4 expandtab :
