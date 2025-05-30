# Copyright 2016-2018, Pulumi Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Utility functions and classes for testing the Python language host.
"""

import asyncio
import logging
import subprocess
import unittest
from collections import namedtuple
from concurrent import futures
from inspect import signature
from os import path

import grpc
from google.protobuf import empty_pb2, struct_pb2
from pulumi.runtime import proto, rpc
from pulumi.runtime.proto import (
    engine_pb2,
    engine_pb2_grpc,
    language_pb2_grpc,
    provider_pb2,
    resource_pb2_grpc,
)

# gRPC by default logs exceptions to the root `logging` logger. We don't
# want this because it spews garbage to stderr and messes up our beautiful
# test output. Just turn it off.
logging.disable(level=logging.CRITICAL)

# _MAX_RPC_MESSAGE_SIZE raises the gRPC Max Message size from `4194304` (4mb) to `419430400` (400mb)
_MAX_RPC_MESSAGE_SIZE = 1024 * 1024 * 400
_GRPC_CHANNEL_OPTIONS = [("grpc.max_receive_message_length", _MAX_RPC_MESSAGE_SIZE)]


class LanghostMockResourceMonitor(proto.ResourceMonitorServicer):
    """
    Implementation of proto.ResourceMonitorServicer for use in tests. Tests themselves
    should not have to interact with this class.

    This class encapsulates all gRPC details so that test authors only have to override
    the `invoke`, `read_resource`, `register_resource`, and `register_resource_outputs`
    functions on `LanghostTest` in order to get custom behavior.
    """

    def __init__(self, langhost_test, dryrun):
        self.reg_count = 0
        self.registrations = {}
        self.dryrun = dryrun
        self.langhost_test = langhost_test

    def Invoke(self, request, context):
        args = rpc.deserialize_properties(request.args)
        failures, ret = self.langhost_test.invoke(
            context, request.tok, args, request.provider, request.version
        )
        failures_rpc = list(
            map(
                lambda fail: provider_pb2.CheckFailure(
                    property=fail["property"], reason=fail["reason"]
                ),
                failures,
            )
        )

        loop = asyncio.new_event_loop()
        ret_proto = loop.run_until_complete(rpc.serialize_properties(ret, {}))
        loop.close()
        fields = {"failures": failures_rpc, "return": ret_proto}
        return proto.InvokeResponse(**fields)

    def ReadResource(self, request, context):
        type_ = request.type
        name = request.name
        id_ = request.id
        parent = request.parent
        state = rpc.deserialize_properties(request.properties)
        dependencies = sorted(list(request.dependencies))
        provider = request.provider
        version = request.version
        outs = self.langhost_test.read_resource(
            context, type_, name, id_, parent, state, dependencies, provider, version
        )
        if "properties" in outs:
            loop = asyncio.new_event_loop()
            props_proto = loop.run_until_complete(
                rpc.serialize_properties(outs["properties"], {})
            )
            loop.close()
        else:
            props_proto = None
        return proto.ReadResourceResponse(urn=outs.get("urn"), properties=props_proto)

    def RegisterResource(self, request, context):
        type_ = request.type
        name = request.name
        props = rpc.deserialize_properties(request.object)
        deps = sorted(list(request.dependencies))
        parent = request.parent
        custom = request.custom
        protect = request.protect
        provider = request.provider
        delete_before_replace = request.deleteBeforeReplace
        ignore_changes = sorted(list(request.ignoreChanges))
        version = request.version
        import_ = request.importId
        replace_on_changes = sorted(list(request.replaceOnChanges))
        providers = request.providers
        source_position = request.sourcePosition

        property_dependencies = {}
        for key, value in request.propertyDependencies.items():
            property_dependencies[key] = sorted(list(value.urns))

        if type_ != "pulumi:pulumi:Stack":
            outs = self.langhost_test.register_resource(
                context,
                self.dryrun,
                type_,
                name,
                props,
                deps,
                parent,
                custom,
                protect,
                provider,
                property_dependencies,
                delete_before_replace,
                ignore_changes,
                version,
                import_,
                replace_on_changes,
                providers,
                source_position,
            )
            if outs.get("urn"):
                urn = outs["urn"]
                self.registrations[urn] = {"type": type_, "name": name, "props": props}

            self.reg_count += 1
        else:
            # Record the Stack's registration so that it can be the target of register_resource_outputs
            # later on.
            urn = self.langhost_test.make_urn(type_, "teststack")
            self.registrations[urn] = {"type": type_, "name": "somestack", "props": {}}

            return proto.RegisterResourceResponse(urn=urn, id="teststack", object=None)
        if "object" in outs:
            loop = asyncio.new_event_loop()
            obj_proto = loop.run_until_complete(
                rpc.serialize_properties(outs["object"], {})
            )
            loop.close()
        else:
            obj_proto = None

        output_property_dependencies = None
        if "propertyDependencies" in outs:
            output_property_dependencies = {}
            for key, urns in outs["propertyDependencies"].items():
                output_property_dependencies[key] = (
                    proto.RegisterResourceResponse.PropertyDependencies(urns=urns)
                )

        return proto.RegisterResourceResponse(
            urn=outs.get("urn"),
            id=outs.get("id"),
            object=obj_proto,
            propertyDependencies=output_property_dependencies,
        )

    def RegisterResourceOutputs(self, request, context):
        urn = request.urn
        outs = rpc.deserialize_properties(request.outputs)
        res = self.registrations.get(urn)
        if res:
            self.langhost_test.register_resource_outputs(
                context, self.dryrun, urn, res["type"], res["name"], res["props"], outs
            )
        return empty_pb2.Empty()


class MockEngine(proto.EngineServicer):
    def __init__(self):
        self.messages = []

    """
    Implementation of the proto.EngineServicer protocol for use in tests. Like the
    above class, we encapsulate all gRPC details here so that test writers only have
    to override methods on LanghostTest.
    """

    def Log(self, request, context):
        self.messages.append(request.message)
        if request.severity == engine_pb2.ERROR:
            print(f"error: {request.message}")
        return empty_pb2.Empty()


ResourceMonitorEndpoint = namedtuple(
    "ResourceMonitorEndpoint", ["engine", "monitor", "server", "port"]
)
LanguageHostEndpoint = namedtuple("LanguageHostEndpoint", ["process", "port"])


class LanghostTest(unittest.TestCase):
    """
    Base class of all end-to-end tests of the Python language host.

    The `run_test` method on this class executes a test by mocking the
    Pulumi Engine gRPC endpoint and running the language host exactly the same
    way that the Pulumi CLI will. Once the program completes and the language host
    exits, `run_test` will assert the expected errors and/or resource registration
    counts specified by the individual test.

    Check out README.md in this directory for more details.
    """

    def run_test(
        self,
        project=None,
        stack=None,
        program=None,
        args=None,
        config=None,
        expected_resource_count=None,
        expected_error=None,
        expected_stderr_contains=None,
        expected_bail=None,
        expected_log_message=None,
        organization=None,
    ):
        """
        Runs a language host test. The basic flow of a language host test is that
        a test is launched using the real language host while mocking out the resource
        monitor RPC interface and, once the program completes, assertions are made about
        the sorts of resource monitor calls that were made.

        :param project: The name of the project in which the program will run.
        :param stack: The name of the stack in which the program will run.
        :param program: The path to the program the langhost should execute.
        :param args: Arguments to the program.
        :param config: Configuration keys for the program.
        :param expected_resource_count: The number of resources this program is expected to create.
        :param expected_error: If present, the expected error that should arise when running this program.
        :param expected_stderr_contains: If present, the standard error of the process should contain this string
        """
        # For each test case, we'll do a preview followed by an update.
        for dryrun in [True, False]:
            # For these tests, we are the resource monitor. The `invoke`, `read_resource`,
            # `register_resource`, and `register_resource_outputs` methods on this class
            # will be used to implement the resource monitor gRPC interface. The tests
            # that we spawn will connect to us directly.
            monitor = self._create_mock_resource_monitor(dryrun)

            # Now we'll launch the language host, which will in turn launch code that drives
            # the test.
            langhost = self._create_language_host(monitor.port)

            # Run the program with the langhost we just launched.
            with grpc.insecure_channel(
                "localhost:%d" % langhost.port, options=_GRPC_CHANNEL_OPTIONS
            ) as channel:
                grpc.channel_ready_future(channel).result()
                stub = language_pb2_grpc.LanguageRuntimeStub(channel)
                result = self._run_program(
                    stub,
                    monitor,
                    project,
                    stack,
                    program,
                    args,
                    config,
                    dryrun,
                    organization,
                )

            # Tear down the language host process we just spun up.
            langhost.process.kill()
            stdout, stderr = langhost.process.communicate()
            monitor.server.stop(0)
            if not expected_stderr_contains and (stdout or stderr):
                print("PREVIEW:" if dryrun else "UPDATE:")
                print("stdout:", stdout)
                print("stderr:", stderr)

            # If we expected an error, assert that we saw it. Otherwise assert
            # that there wasn't an error.
            expected = expected_error or ""
            self.assertEqual(result.error, expected)

            expected_bail = expected_bail or False
            self.assertEqual(result.bail, expected_bail)

            if expected_log_message:
                for message in monitor.engine.messages:
                    if expected_log_message in message:
                        break
                else:
                    print("log messages:", monitor.engine.messages)
                    self.fail("expected log message '" + expected_log_message + "'")

            if expected_stderr_contains:
                if expected_stderr_contains not in str(stderr):
                    print("stderr:", str(stderr))
                    self.fail(
                        "expected stderr to contain '" + expected_stderr_contains + "'"
                    )

            if expected_resource_count is not None:
                self.assertEqual(expected_resource_count, monitor.monitor.reg_count)

    def invoke(self, _ctx, token, args, provider, _version):
        """
        Method corresponding to the `Invoke` resource monitor RPC call.
        Override for custom behavior or assertions.

        Returns a tuple of a list of errors that returned and the returned
        object, if the call was successful.
        """
        return ([], {})

    def read_resource(
        self, ctx, ty, name, _id, parent, state, dependencies, provider, version
    ):
        """
        Method corresponding to the `ReadResource` resource monitor RPC call.
        Override for custom behavior or assertions.

        Returns a resource that was read from existing state.
        """
        return {}

    def register_resource(
        self,
        _ctx,
        _dry_run,
        ty,
        name,
        _resource,
        _dependencies,
        _parent,
        _custom,
        protect,
        _provider,
        _property_deps,
        _delete_before_replace,
        _ignore_changes,
        _version,
        _import,
        _replace_on_changes,
        _providers,
        source_position,
    ):
        """
        Method corresponding to the `RegisterResource` resource monitor RPC call.
        Override for custom behavior or assertions.

        Returns a resource that was just created.
        """
        return {}

    def register_resource_outputs(
        self, _ctx, _dry_run, _urn, _type, _name, _resource, _outputs
    ):
        """
        Method corresponding to the `RegisterResourceOutputs` resource monitor RPC call.
        Override for custom behavior or assertirons.

        Returns None.
        """
        pass

    def make_urn(self, type_, name):
        """
        Makes an URN from a given resource type and name.
        """
        return "%s::%s" % (type_, name)

    def base_path(self):
        """
        Returns the base path for language host tests.
        """
        return path.dirname(__file__)

    def _create_mock_resource_monitor(self, dryrun):
        monitor = LanghostMockResourceMonitor(self, dryrun)
        engine = MockEngine()
        server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=4), options=_GRPC_CHANNEL_OPTIONS
        )

        resource_pb2_grpc.add_ResourceMonitorServicer_to_server(monitor, server)
        engine_pb2_grpc.add_EngineServicer_to_server(engine, server)

        port = server.add_insecure_port(address="127.0.0.1:0")
        server.start()
        return ResourceMonitorEndpoint(engine, monitor, server, port)

    def _create_language_host(self, port):
        exec_path = path.join(
            path.dirname(__file__),
            "..",
            "..",
            "..",
            "cmd",
            "pulumi-language-python-exec",
        )
        proc = subprocess.Popen(
            [
                "pulumi-language-python",
                "--use-executor",
                exec_path,
                "localhost:%d" % port,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # The first line of output is the port that the language host gRPC server is listening on.
        first_line = proc.stdout.readline()
        try:
            return LanguageHostEndpoint(proc, int(first_line))
        except ValueError:
            proc.kill()
            stdout, stderr = proc.communicate()
            print("language host did not respond with port")
            print("stdout: ")
            print(stdout)
            print("stderr: ")
            print(stderr)
            raise

    def _run_program(
        self, stub, monitor, project, stack, program, args, config, dryrun, organization
    ):
        args = {}

        # Assume program is the path to the project, i.e. root, program directory, and pwd.
        args["info"] = proto.ProgramInfo(
            root_directory=program, program_directory=program, entry_point="."
        )

        args["monitor_address"] = "localhost:%d" % monitor.port
        args["project"] = project or "project"
        args["stack"] = stack or "stack"
        args["program"] = "."
        args["organization"] = organization
        args["pwd"] = program

        if args:
            args["args"] = args

        if config:
            args["config"] = config

        args["dryRun"] = dryrun
        request = proto.RunRequest(**args)
        self.assertTrue(request.IsInitialized())
        resp = stub.Run(request)
        return resp
