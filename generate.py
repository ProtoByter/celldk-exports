import os
import json
import inspect
from itertools import chain
import sys
from subprocess import run
import jsonschema
from jsonschema import validate
from enum import Enum
import shutil


def validate_json(json_data, json_schema):
    execute_api_schema = json_schema()

    try:
        validate(instance=json_data, schema=execute_api_schema)
    except jsonschema.exceptions.ValidationError as err:
        print(err)
        return False

    return True


def get_export_schema():
    with open("syscall_def.json", "r") as f:
        schema = json.load(f)
    return schema


def validate_export_def(json_data):
    return validate_json(json_data, get_export_schema)


def get_lib_def_schema():
    with open("library_def.json", "r") as f:
        schema = json.load(f)
    return schema


def validate_lib_def(json_data):
    return validate_json(json_data, get_lib_def_schema)


class LibType(Enum):
    Syscall = 0
    PRX = 1


class Library:
    def __init__(self, _name: str, _type: LibType):
        self.type = _type
        self.name = _name
        self.files = {}

    def write_to_disk(self, prefix: str):
        try:
            os.mkdir(os.path.join(prefix, self.name))
            os.mkdir(os.path.join(prefix, self.name, "include"))
        except FileExistsError:
            pass

        for file in self.files.keys():
            with open(prefix + self.name + "/" + file, "w") as f:
                f.write(self.files[file])


def c_generator():
    generated_libraries = {}

    header_files = {
        "syscalls": inspect.cleandoc("""#ifndef LV2_SYSCALLS_H
            #define LV2_SYSCALLS_H
            """)
    }
    assembly_file = ""

    header_fmt_str = inspect.cleandoc("""
    // {}
    #define {}_ID {}
    
    /*! \\brief {}.
        {}
    {}*/
    
    {} {}({});\n
    """)

    assembly_fmt_str = inspect.cleandoc("""
        .globl  {}
    
    {}:
        li 11, {}
        sc
        blr
    """)

    cmake_syscall_file = inspect.cleandoc("""
    cmake_minimum_required(VERSION 3.0)
    project({}_syscalls LANGUAGES C ASM)
    
    if(CMAKE_TOOLCHAIN_FILE STREQUAL "")
        message(FATAL_ERROR "The PS3DK Toolchain File must be used to build this library")
    endif()
    
    add_library({}_syscalls STATIC syscalls.h syscalls.S)
    """)

    cmake_prx_file = inspect.cleandoc("""
    cmake_minimum_required(VERSION 3.0)
    project({}_prx LANGUAGES C)
    
    if(CMAKE_TOOLCHAIN_FILE STREQUAL "")
        message(FATAL_ERROR "The PS3DK Toolchain File must be used to build this library")
    endif()
    
    add_library({}_prx STATIC ../common/export.S ../common/libexport.c)
    """)

    prx_def_file = inspect.cleandoc("""
    EXPORT({}, {})
    """)

    prx_config_file = inspect.cleandoc("""
    #define LIBRARY_NAME		"{}"
    #define LIBRARY_SYMBOL		{}
    
    #define LIBRARY_HEADER_1	{}
    #define LIBRARY_HEADER_2	{}
    """)

    search_dirs = {}

    for file in [x for x in os.listdir("specs") if os.path.isfile(os.path.join("specs", x))]:
        if file.split(".")[-1] == "json":
            with open(f"specs/{file}") as f:
                lib_def = json.load(f)
                if not validate_lib_def(lib_def):
                    print(f"Library definition {file} is not conformant to schema, skipping")
                    continue

                if lib_def["path"] not in search_dirs.keys():
                    search_dirs[lib_def["path"]] = []

                if "syscall" in lib_def["lib_type"]:
                    sc_lib = Library(f"{lib_def['name']}_syscalls", LibType.Syscall)
                    generated_libraries[sc_lib.name] = sc_lib
                    generated_libraries[sc_lib.name].files["CMakeLists.txt"] = cmake_syscall_file.format(
                        sc_lib.name, sc_lib.name, "{}"
                    )

                    generated_libraries[sc_lib.name].files["syscalls.S"] = ""
                    generated_libraries[sc_lib.name].files["syscalls.h"] = "#include <ppu-types.h>\n\n"

                    search_dirs[lib_def["path"]].append(sc_lib.name)

                if "sprx" in lib_def["lib_type"]:
                    sprx_lib = Library(f"{lib_def['name']}_sprx", LibType.PRX)
                    generated_libraries[sprx_lib.name] = sprx_lib
                    generated_libraries[sprx_lib.name].files["CMakeLists.txt"] = cmake_prx_file.format(
                        sprx_lib.name, sprx_lib.name, "{}"
                    )

                    generated_libraries[sprx_lib.name].files["exports.h"] = ""
                    generated_libraries[sprx_lib.name].files["config.h"] = prx_config_file.format(
                        lib_def["prx_info"]["symbol"], lib_def["prx_info"]["symbol"],
                        lib_def["prx_info"]["header1"], lib_def["prx_info"]["header2"],
                    )

                    search_dirs[lib_def["path"]].append(sprx_lib.name)

    for search_dir in search_dirs:
        for root, dirs, files in os.walk(f"specs/{search_dir}", topdown=False):
            for file in files:
                if file.split(".")[-1] == "json":
                    with open(os.path.join(root, file)) as f:
                        spec = json.load(f)
                        if not validate_export_def(spec):
                            print(f"{file} isn't conformant to the schema, skipping")
                            continue

                        requirements = ""
                    if len(spec["flags"]) != 0:
                        requirements += inspect.cleandoc("""
                            Required flags:
                        """)
                        for flag in spec["flags"]:
                            requirements += f"\n- {flag}\n\n"

                    cex_support = "×"
                    dex_support = "×"
                    decr_support = "×"

                    if "CEX" in spec["firmwares"]:
                        cex_support = "✓"

                    if "DEX" in spec["firmwares"]:
                        dex_support = "✓"

                    if "DECR" in spec["firmwares"]:
                        decr_support = "✓"

                    requirements += inspect.cleandoc(f"""
                        Firmware support:
                        |Firmware|Supported|
                        |--------|---------|
                        |CEX|{cex_support}|
                        |DEX|{dex_support}|
                        |DECR|{decr_support}|
                    """)

    try:
        os.mkdir("generated")
        os.mkdir("generated/sprx")
        shutil.copytree("common", "generated/sprx/common")
        os.mkdir("generated/syscalls")
    except FileExistsError:
        pass

    cmake_libs_combined = inspect.cleandoc("""
    cmake_minimum_required(VERSION 3.0)
    project(celldk_hi_libraries)
    """) + "\n\n"

    for lib in generated_libraries.values():
        if lib.type == LibType.PRX:
            cmake_libs_combined += f"add_subdirectory(sprx/{lib.name})\n"
            lib.write_to_disk("generated/sprx/")
        else:
            cmake_libs_combined += f"add_subdirectory(syscalls/{lib.name})\n"
            lib.write_to_disk("generated/syscalls/")

    with open("generated/CMakeLists.txt", "w") as f:
        f.write(cmake_libs_combined)


def ask_param(question, default=None, no_response=False):
    if default is not None:
        tmp = input(f"{question} [{default}]: ").strip("\n")
        if tmp == "":
            return default
        else:
            return tmp

    else:
        tmp = input(f"{question}: ").strip("\n")
        if tmp == "" and not no_response:
            print("This parameter is required, please enter a value")
            return ask_param(question, default, no_response)
        elif tmp == "" and no_response:
            return None
        else:
            return tmp


def json_upgrade():
    print("Upgrading JSON files")
    files_and_roots = [(files, root) for root, _, files in os.walk("specs", topdown=False)]

    for files, root in files_and_roots:
        for file in files:
            print(f"Upgrading file: {file}")
            with open(os.path.join(root, file), "r") as f:
                data = json.load(f)

            if "id" in data.keys():
                prx_id = ask_param("PRX ID", no_response=True)

                data["ids"] = {
                    "syscall_id": data["id"]
                }

                if prx_id is not None:
                    data["ids"]["prx_id"] = prx_id

                del data["id"]

            with open(os.path.join(root, file), "w") as f:
                json.dump(data, f)

            run(['clang-format', '-i', os.path.join(root, file)])


def json_generator(argv):
    if sys.argv[1] == "upgrade":
        json_upgrade()
    else:
        if sys.argv[1] != "add":
            fname = sys.argv[1]
        else:
            fname = ask_param("File name")

        func_name = ask_param("Function name", default=f"sys_{fname[:-5]}")

        spec = {
            "name": func_name,
            "id": int(ask_param("ID")),
            "returns": ask_param("Return type", default="void"),
            "brief": ask_param("Description"),
            "class": ask_param("Class", default=f"{'_'.join(func_name.split('_')[:2])}"),
            "params": [],
            "flags": [],
            "firmwares": []
        }

        i = 0

        while True:
            name = ask_param(f"Parameter {i + 1} name", no_response=True)

            if name is None:
                break

            param = {
                "name": name,
                "type": ask_param(f"Parameter {i + 1} type", no_response=True),
                "description": ask_param(f"Parameter {i + 1} description", no_response=True)
            }

            if param["name"] is None or param["type"] is None or param["description"] is None:
                break
            else:
                spec["params"].append(param)

            i += 1

        for firmware in ["CEX", "DEX", "DECR"]:
            if ask_param(f"Does this function work on {firmware} (y/n)") == "y":
                spec["firmwares"].append(firmware)

        while True:
            flag = ask_param("Enter a required flag", no_response=True)

            if flag is None:
                break
            else:
                spec["flags"].append(flag)

        with open(f"specs/{fname}", "w") as f:
            json.dump(spec, f)

        run(['clang-format', '-i', f'specs/{fname}'])


if __name__ == '__main__':
    if len(sys.argv) != 1:
        json_generator(sys.argv)
    else:
        c_generator()
