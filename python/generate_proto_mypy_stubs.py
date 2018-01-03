#!/usr/bin/env python
"""Protoc Plugin to generate mypy stubs. Loosely based on @zbarsky's go implementation"""
from __future__ import (
    absolute_import,
    division,
    print_function,
)

import sys
from collections import defaultdict
from contextlib import contextmanager

import google.protobuf.descriptor_pb2 as d_typed
import six
from google.protobuf.compiler import plugin_pb2 as plugin

MYPY = False
if MYPY:
    from typing import (
        Any,
        Dict,
        Generator,
        List,
        Set,
        Text,
    )

# Hax to get around fact that google protobuf libraries aren't in typeshed yet
d = d_typed  # type: Any

GENERATED = "@ge" + "nerated"  # So phabricator doesn't think this file is generated
HEADER = "# {} by generate_proto_mypy_stubs.py.  Do not edit!\n".format(GENERATED)

class PkgWriter(object):
    """Writes a single pyi file"""

    def __init__(self, fd, descriptors):
        # type: (d.FileDescriptorProto, Descriptors) -> None
        self.fd = fd
        self.descriptors = descriptors
        self.lines = []  # type: List[Text]
        self.indent = ""

        # dictionary of x->y for `from {x} import {y}`
        self.imports = defaultdict(set)  # type: Dict[Text, Set[Text]]
        self.locals = set()  # type: Set[Text]

    def _import(self, path, name):
        # type: (Text, Text) -> Text
        """Imports a stdlib path and returns a handle to it
        eg. self._import("typing", "Optional") -> "Optional"
        """
        imp = path.replace('/', '.')
        self.imports[imp].add(name)
        return name

    def _import_message(self, type_name):
        # type: (d.FieldDescriptorProto) -> Text
        """Import a referenced message and return a handle"""
        name = type_name
        if name[0] == '.' and name[1].isupper():
            # Message defined in this file
            return name[1:]

        message_fd = self.descriptors.message_to_fd[name]
        if message_fd.name == self.fd.name:
            # message defined in this package
            split = type_name.split('.')
            for i, segment in enumerate(split):
                if segment and segment[0].isupper():
                    return ".".join(split[i:])

        # Not in package. Must import
        split = type_name.split(".")
        for i, segment in enumerate(split):
            if segment and segment[0].isupper():
                assert message_fd.name.endswith('.proto')
                import_name = self._import(message_fd.name[:-6], segment)
                remains = ".".join(split[i + 1:])
                if not remains:
                    return import_name
                raise AssertionError("Don't support nested imports yet")
                # return new_nested_import(import_name, remains)

        raise AssertionError("Could not parse local name " + name)

    @contextmanager
    def _indent(self):
        # type: () -> Generator
        self.indent = self.indent + "    "
        yield
        self.indent = self.indent[:-4]

    def _write_line(self, line, *args):
        # type: (Text, *Text) -> None
        self.lines.append(self.indent + line.format(*args))

    def write_enums(self, enums):
        # type: (List[d.EnumDescriptorProto]) -> None
        l = self._write_line
        for enum in enums:
            l("class {}(int):", enum.name)
            with self._indent():
                l("@classmethod")
                l("def Name(cls, number: int) -> str: ...")
                l("@classmethod")
                l("def Value(cls, name: str) -> int: ...")
                l("@classmethod")
                l("def keys(cls) -> {}[str]: ...",
                    self._import("typing", "List"))
                l("@classmethod")
                l("def values(cls) -> {}[int]: ...",
                    self._import("typing", "List"))
                l("@classmethod")
                l("def items(cls) -> {}[{}[str, int]]: ...",
                    self._import("typing", "List"),
                    self._import("typing", "Tuple"))

            for val in enum.value:
                l("{} = {}({}, {})", val.name, self._import("typing", "cast"), enum.name, val.number)
            l("")

    def write_messages(self, messages, prefix):
        # type: (List[d.DescriptorProto], Text) -> None
        l = self._write_line
        message_class = self._import("google.protobuf.message", "Message")

        for desc in messages:
            self.locals.add(desc.name)
            qualified_name = prefix + desc.name
            l("class {}({}):", desc.name, message_class)
            with self._indent():
                # Nested enums/messages
                self.write_enums(desc.enum_type)
                self.write_messages(desc.nested_type, qualified_name + ".")

                # Scalar fields
                for field in [f for f in desc.field if is_scalar(f)]:
                    if field.label == d.FieldDescriptorProto.LABEL_REPEATED:
                        container = self._import("mypy", "RepeatedScalarFieldContainer")
                        l("{} = ... # type: {}[{}]", field.name, container, self.python_type(field))
                    else:
                        l("{} = ... # type: {}", field.name, self.python_type(field))
                l("")

                # Getters for non-scalar fields
                for field in [f for f in desc.field if not is_scalar(f)]:
                    l("@property")
                    if field.label == d.FieldDescriptorProto.LABEL_REPEATED:
                        container = self._import("mypy", "RepeatedScalarFieldContainer")
                        l("def {}(self) -> {}[{}]: ...", field.name, container, self.python_type(field))
                    else:
                        l("def {}(self) -> {}: ...", field.name, self.python_type(field))
                    l("")

                # Constructor
                l("def __init__(self,")
                with self._indent():
                    # Required args
                    for field in [f for f in desc.field if f.label == d.FieldDescriptorProto.LABEL_REQUIRED]:
                        l("{} : {},", field.name, self.python_type(field))
                    for field in [f for f in desc.field if f.label != d.FieldDescriptorProto.LABEL_REQUIRED]:
                        if field.label == d.FieldDescriptorProto.LABEL_REPEATED:
                            l("{} : {}[{}] = None,", field.name,
                              self._import("typing", "Iterable"), self.python_type(field))
                        else:
                            l("{} : {} = None,", field.name, self.python_type(field))
                    l(") -> None: ...")

                # Standard message methods
                l("@classmethod")
                l("def FromString(cls, s: bytes) -> {}: ...", qualified_name)
                l("def MergeFrom(self, other_msg: {}) -> None: ...", message_class)
                l("def CopyFrom(self, other_msg: {}) -> None: ...", message_class)
            l("")

    def write_services(self, services):
        # type: (d.ServiceDescriptorProto) -> None
        l = self._write_line

        for service in services:
            l("class {}({}):", service.name + "_Stub", self._import("google.protobuf.service", "Service"))
            with self._indent():
                l("def __init__(self, rpc_channel: {}) -> None: ...",
                  self._import("google.protobuf.service", "RpcChannel"))
                for method in service.method:
                    l("def {}(self,", method.name)
                    with self._indent():
                        l("rpc_controller: {},", self._import("google.protobuf.service", "RpcController"))
                        l("request: {},", self._import_message(method.input_type))
                        l("done: {}[{}[[{}], None]],",
                          self._import("typing", "Optional"),
                          self._import("typing", "Callable"),
                          self._import_message(method.output_type))
                    l(") -> {}[{}]: ...", self._import("concurrent.futures", "Future"), self._import_message(method.output_type))

    def python_type(self, field):
        # type: (d.FieldDescriptorProto) -> Text
        mapping = {
            d.FieldDescriptorProto.TYPE_DOUBLE: lambda: "float",
            d.FieldDescriptorProto.TYPE_FLOAT: lambda: "float",

            d.FieldDescriptorProto.TYPE_INT64: lambda: "int",
            d.FieldDescriptorProto.TYPE_UINT64: lambda: "int",
            d.FieldDescriptorProto.TYPE_FIXED64: lambda: "int",
            d.FieldDescriptorProto.TYPE_SFIXED64: lambda: "int",
            d.FieldDescriptorProto.TYPE_SINT64: lambda: "int",
            d.FieldDescriptorProto.TYPE_INT32: lambda: "int",
            d.FieldDescriptorProto.TYPE_UINT32: lambda: "int",
            d.FieldDescriptorProto.TYPE_FIXED32: lambda: "int",
            d.FieldDescriptorProto.TYPE_SFIXED32: lambda: "int",
            d.FieldDescriptorProto.TYPE_SINT32: lambda: "int",

            d.FieldDescriptorProto.TYPE_BOOL: lambda: "bool",
            d.FieldDescriptorProto.TYPE_STRING: lambda: self._import("typing", "Text"),
            d.FieldDescriptorProto.TYPE_BYTES: lambda: "bytes",

            d.FieldDescriptorProto.TYPE_ENUM: lambda: self._import_message(field.type_name),
            d.FieldDescriptorProto.TYPE_MESSAGE: lambda: self._import_message(field.type_name),
            d.FieldDescriptorProto.TYPE_GROUP: lambda: self._import_message(field.type_name),
        }

        assert field.type in mapping, "Unrecognized type: " + field.type
        return mapping[field.type]()

    def write(self):
        # type: () -> Text
        imports = []
        for pkg, items in six.iteritems(self.imports):
            imports.append(u"from {} import (".format(pkg))
            for item in items:
                imports.append(u"    {},".format(item))
            imports.append(u")\n")

        return "\n".join(imports + self.lines)

def is_scalar(fd):
    # type: (d.FileDescriptorProto) -> bool
    return not (
        fd.type == d.FieldDescriptorProto.TYPE_MESSAGE or
        fd.type == d.FieldDescriptorProto.TYPE_GROUP
    )

def generate_mypy_stubs(descriptors, response):
    # type: (Descriptors, plugin.CodeGeneratorResponse) -> plugin.CodeGeneratorResponse
    for name, fd in six.iteritems(descriptors.to_generate):
        pkg_writer = PkgWriter(fd, descriptors)
        pkg_writer.write_enums(fd.enum_type)
        pkg_writer.write_messages(fd.message_type, "")
        pkg_writer.write_services(fd.service)

        assert name == fd.name
        assert fd.name.endswith('.proto')
        output = response.file.add()
        output.name = fd.name[:-6] + '_pb2.pyi'
        output.content = HEADER + pkg_writer.write()
        print("Writing mypy to", output.name, file=sys.stderr)

class Descriptors(object):

    def __init__(self, request):
        # type: (plugin.CodeGeneratorRequest) -> None
        files = {f.name: f for f in request.proto_file}
        to_generate = {n: files[n] for n in request.file_to_generate}
        self.files = files  # type: Dict[Text, d.FileDescriptorProto]
        self.to_generate = to_generate  # type: Dict[Text, d.FileDescriptorProto]

        self.message_to_fd = {}  # type: Dict[Text, d.FileDescriptorProto]

        def _add_enums(enums, prefix, fd):
            # type: (d.EnumDescriptorProto, Text) -> None
            for enum in enums:
                self.message_to_fd[prefix + enum.name] = fd

        def _add_messages(messages, prefix, fd):
            # type: (d.DescriptorProto, Text) -> None
            for message in messages:
                self.message_to_fd[prefix + message.name] = fd
                sub_prefix = prefix + message.name + "."
                _add_messages(message.nested_type, sub_prefix, fd)
                _add_enums(message.enum_type, sub_prefix, fd)

        for fd in request.proto_file:
            start_prefix = "." + fd.package + "."
            _add_messages(fd.message_type, start_prefix, fd)
            _add_enums(fd.enum_type, start_prefix, fd)

def main():
    # type: () -> None
    # Read request message from stdin
    if six.PY3:
        data = sys.stdin.buffer.read()
    else:
        data = sys.stdin.read()

    # Parse request
    request = plugin.CodeGeneratorRequest()
    request.ParseFromString(data)

    # Create response
    response = plugin.CodeGeneratorResponse()

    # Generate mypy
    generate_mypy_stubs(Descriptors(request), response)

    # Serialise response message
    output = response.SerializeToString()

    # Write to stdout
    sys.stdout.write(output)

if __name__ == '__main__':
    main()