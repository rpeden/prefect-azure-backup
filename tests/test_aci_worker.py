import uuid
from typing import List, Tuple, Union
from unittest.mock import MagicMock, Mock

import dateutil.parser
import pytest
from anyio.abc import TaskStatus
from azure.core.exceptions import HttpResponseError
from azure.identity import ClientSecretCredential
from azure.mgmt.containerinstance.models import (
    ImageRegistryCredential,
)
from azure.mgmt.resource import ResourceManagementClient
from prefect.client.schemas import FlowRun
from prefect.infrastructure.docker import DockerRegistry
from prefect.server.schemas.core import Flow
from prefect.testing.utilities import AsyncMock
from pydantic import SecretStr

import prefect_azure.container_instance
from prefect_azure import AzureContainerInstanceCredentials
from prefect_azure.workers.container_instance import AzureContainerVariables  # noqa
from prefect_azure.workers.container_instance import (
    AzureContainerJobConfiguration,
    AzureContainerWorker,
    AzureContainerWorkerResult,
    ContainerGroupProvisioningState,
    ContainerRunState,
)


# Helper functions
def credential_values(
    credentials: AzureContainerInstanceCredentials,
) -> Tuple[str, str, str]:
    """
    Helper function to extract values from an Azure container instances
    credential block

    Args:
        credentials: The credential to extract values from

    Returns:
        A tuple containing (client_id, client_secret, tenant_id) from
        the credentials block
    """
    return (
        credentials.client_id,
        credentials.client_secret.get_secret_value(),
        credentials.tenant_id,
    )


def create_mock_container_group(state: str, exit_code: Union[int, None]):
    """
    Creates a mock container group with a single container to serve as a stand-in for
    an Azure ContainerInstanceManagementClient's container_group property.

    Args:
        state: The state the single container in the group should report.
        exit_code: The container's exit code, or None

    Returns:
        A mock container group.
    """
    container_group = Mock()
    container = Mock()
    container.instance_view.current_state.state = state
    container.instance_view.current_state.exit_code = exit_code
    containers = [container]
    container_group.containers = containers
    # Azure assigns all provisioned container groups a stringified
    # UUID name.
    container_group.name = str(uuid.uuid4())
    return container_group


async def create_job_configuration(
    aci_credentials, worker_flow_run, overrides={}, run_prep=True
):
    """
    Returns a basic initialized ACI infrastructure block suitable for use
    in a variety of tests.
    """
    values = {
        "command": "test",
        "aci_credentials": aci_credentials,
        "resource_group_name": "test_group",
        "subscription_id": SecretStr("sub_id"),
        "name": None,
        "task_watch_poll_interval": 0.05,
        "stream_output": False,
    }

    for k, v in overrides.items():
        values = {**values, k: v}

    container_instance_variables = AzureContainerVariables(**values)

    json_config = {
        "job_configuration": AzureContainerJobConfiguration.json_template(),
        "variables": container_instance_variables.dict(),
    }

    container_instance_configuration = (
        await AzureContainerJobConfiguration.from_template_and_values(
            json_config, values
        )
    )

    if run_prep:
        container_instance_configuration.prepare_for_flow_run(worker_flow_run)

    return container_instance_configuration


def get_command_from_deployment_parameters(parameters):
    deployment_arm_template = parameters.template
    # We're only interested in the first resource, because our ACI
    # flow run container groups only have a single container by default.
    deployment_resources = deployment_arm_template["resources"][0]
    deployment_properties = deployment_resources["properties"]
    deployment_containers = deployment_properties["containers"]

    command = deployment_containers[0]["properties"]["command"]
    return command


@pytest.fixture()
def running_worker_container_group():
    """
    A fixture that returns a mock container group simulating a
    a container group that is currently running a flow run.
    """
    container_group = create_mock_container_group(state="Running", exit_code=None)
    container_group.provisioning_state = ContainerGroupProvisioningState.SUCCEEDED
    return container_group


@pytest.fixture()
def completed_worker_container_group():
    """
    A fixture that returns a mock container group simulating a
    a container group that successfully completed its flow run.
    """
    container_group = create_mock_container_group(state="Terminated", exit_code=0)
    container_group.provisioning_state = ContainerGroupProvisioningState.SUCCEEDED

    return container_group


# Fixtures
@pytest.fixture
def aci_credentials(monkeypatch):
    client_id = "test_client_id"
    client_secret = "test_client_secret"
    tenant_id = "test_tenant_id"

    mock_credential = Mock(wraps=ClientSecretCredential, return_value=Mock())

    monkeypatch.setattr(
        prefect_azure.credentials,
        "ClientSecretCredential",
        mock_credential,
    )

    credentials = AzureContainerInstanceCredentials(
        client_id=client_id, client_secret=client_secret, tenant_id=tenant_id
    )

    return credentials


@pytest.fixture
def aci_worker(mock_prefect_client, monkeypatch):
    monkeypatch.setattr(
        prefect_azure.workers.container_instance,
        "get_client",
        Mock(return_value=mock_prefect_client),
    )
    return AzureContainerWorker(work_pool_name="test_pool")


@pytest.fixture()
async def job_configuration(aci_credentials, worker_flow_run):
    """
    Returns a basic initialized ACI infrastructure block suitable for use
    in a variety of tests.
    """
    return await create_job_configuration(aci_credentials, worker_flow_run)


@pytest.fixture()
async def raw_job_configuration(aci_credentials, worker_flow_run):
    """
    Returns a basic job configuration suitable for use in a variety of tests.
    ``prepare_for_flow_run`` has not called on the returned configuration, so you
    will need to call it yourself before using the job configuration.
    """
    return await create_job_configuration(
        aci_credentials, worker_flow_run, run_prep=False
    )


@pytest.fixture()
def mock_aci_client(monkeypatch, mock_resource_client):
    """
    A fixture that provides a mock Azure Container Instances client
    """
    container_groups = Mock(name="container_group")
    creation_status_poller = Mock(name="created container groups")
    creation_status_poller_result = Mock(name="created container groups result")
    container_groups.begin_create_or_update.side_effect = (
        lambda *args: creation_status_poller
    )
    creation_status_poller.result.side_effect = lambda: creation_status_poller_result
    creation_status_poller_result.provisioning_state = (
        ContainerGroupProvisioningState.SUCCEEDED
    )
    creation_status_poller_result.name = str(uuid.uuid4())
    container = Mock()
    container.instance_view.current_state.exit_code = 0
    container.instance_view.current_state.state = ContainerRunState.TERMINATED
    containers = Mock(name="containers", containers=[container])
    container_groups.get.side_effect = [containers]
    creation_status_poller_result.containers = [containers]

    aci_client = Mock(container_groups=container_groups)
    monkeypatch.setattr(
        prefect_azure.credentials,
        "ContainerInstanceManagementClient",
        Mock(return_value=aci_client),
    )
    return aci_client


@pytest.fixture()
def mock_prefect_client(monkeypatch, worker_flow):
    """
    A fixture that provides a mock Prefect client
    """
    mock_client = Mock()
    mock_client.read_flow = AsyncMock()
    mock_client.read_flow.return_value = worker_flow

    monkeypatch.setattr(
        prefect_azure.workers.container_instance,
        "get_client",
        Mock(return_value=mock_client),
    )

    return mock_client


@pytest.fixture()
def mock_resource_client(monkeypatch):
    mock_resource_client = MagicMock(spec=ResourceManagementClient)

    def return_group(name: str):
        client = ResourceManagementClient
        return client.models().ResourceGroup(name=name, location="useast")

    mock_resource_client.resource_groups.get = Mock(side_effect=return_group)

    monkeypatch.setattr(
        AzureContainerInstanceCredentials,
        "get_resource_client",
        MagicMock(return_value=mock_resource_client),
    )

    return mock_resource_client


@pytest.fixture
def worker_flow():
    return Flow(id=uuid.uuid4(), name="test-flow")


@pytest.fixture
def worker_flow_run(worker_flow):
    return FlowRun(id=uuid.uuid4(), flow_id=worker_flow.id, name="test-flow-run")


# Tests


async def test_worker_valid_command_validation(aci_credentials, worker_flow_run):
    # ensure the validator allows valid commands to pass through
    command = "command arg1 arg2"

    aci_job_config = await create_job_configuration(
        aci_credentials, worker_flow_run, {"command": command}
    )

    assert aci_job_config.command == command


def test_worker_invalid_command_validation(aci_credentials):
    # ensure invalid commands cause a validation error
    with pytest.raises(ValueError):
        AzureContainerJobConfiguration(
            command=["invalid_command", "arg1", "arg2"],  # noqa
            subscription_id=SecretStr("test"),
            resource_group_name="test",
            aci_credentials=aci_credentials,
        )


async def test_worker_container_client_creation(
    aci_worker, worker_flow_run, job_configuration, aci_credentials, monkeypatch
):
    # verify that the Azure Container Instances client and Azure Resource clients
    # are created correctly.

    mock_azure_credential = Mock(spec=ClientSecretCredential)
    monkeypatch.setattr(
        prefect_azure.credentials,
        "ClientSecretCredential",
        Mock(return_value=mock_azure_credential),
    )

    # don't use the mock_aci_client or mock_resource_client_fixtures, because we want to
    # test the call to the client constructors to ensure the block is calling them
    # with the correct information.
    mock_container_client_constructor = Mock()
    monkeypatch.setattr(
        prefect_azure.credentials,
        "ContainerInstanceManagementClient",
        mock_container_client_constructor,
    )

    mock_resource_client_constructor = Mock()
    monkeypatch.setattr(
        prefect_azure.credentials,
        "ResourceManagementClient",
        mock_resource_client_constructor,
    )

    subscription_id = "test_subscription"
    job_configuration.subscription_id = SecretStr(value=subscription_id)
    with pytest.raises(RuntimeError):
        await aci_worker.run(worker_flow_run, job_configuration)

    mock_resource_client_constructor.assert_called_once_with(
        credential=mock_azure_credential,
        subscription_id=subscription_id,
    )
    mock_container_client_constructor.assert_called_once_with(
        credential=mock_azure_credential,
        subscription_id=subscription_id,
    )


@pytest.mark.usefixtures("mock_aci_client")
async def test_worker_credentials_are_used(
    aci_worker,
    worker_flow_run,
    job_configuration,
    aci_credentials,
    mock_aci_client,
    mock_resource_client,
    monkeypatch,
):
    (client_id, client_secret, tenant_id) = credential_values(aci_credentials)

    mock_client_secret = Mock(name="Mock client secret", return_value=client_secret)
    mock_credential = Mock(wraps=ClientSecretCredential, return_value=Mock())

    monkeypatch.setattr(
        aci_credentials.client_secret, "get_secret_value", mock_client_secret
    )
    monkeypatch.setattr(
        prefect_azure.credentials, "ClientSecretCredential", mock_credential
    )

    with pytest.raises(RuntimeError):
        await aci_worker.run(worker_flow_run, job_configuration)

    mock_client_secret.assert_called_once()
    mock_credential.assert_called_once_with(
        client_id=client_id, client_secret=client_secret, tenant_id=tenant_id
    )


async def test_aci_worker_deployment_call(
    mock_aci_client,
    mock_resource_client,
    completed_worker_container_group,
    aci_worker,
    worker_flow_run,
    job_configuration,
    monkeypatch,
):
    # simulate a successful deployment of a container group to Azure
    monkeypatch.setattr(
        aci_worker,
        "_get_container_group",
        Mock(return_value=completed_worker_container_group),
    )

    mock_poller = Mock()
    # the deployment poller should return a successful deployment
    mock_poller.done = Mock(return_value=True)
    mock_poller_result = MagicMock()
    mock_poller_result.properties.provisioning_state = (
        ContainerGroupProvisioningState.SUCCEEDED
    )
    mock_poller.result = Mock(return_value=mock_poller_result)

    mock_resource_client.deployments.begin_create_or_update = Mock(
        return_value=mock_poller
    )

    # ensure the worker always tries to call the Azure deployments SDK
    # to create the container
    await aci_worker.run(worker_flow_run, job_configuration)
    mock_resource_client.deployments.begin_create_or_update.assert_called_once()


@pytest.mark.parametrize(
    "entrypoint, job_command, expected_template_command",
    [
        # If no entrypoint is provided, just use the command
        (None, "command arg1 arg2", "command arg1 arg2"),
        # entrypoint and command should be combined if both are provided
        (
            "/test/entrypoint.sh",
            "command arg1 arg2",
            "/test/entrypoint.sh command arg1 arg2",
        ),
    ],
)
async def test_worker_uses_entrypoint_correctly_in_template(
    aci_credentials,
    aci_worker,
    worker_flow_run,
    mock_aci_client,
    mock_resource_client,
    monkeypatch,
    entrypoint,
    job_command,
    expected_template_command,
):
    mock_deployment_call = Mock()
    mock_resource_client.deployments.begin_create_or_update = mock_deployment_call

    job_overrides = {
        "entrypoint": entrypoint,
        "command": job_command,
    }

    run_job_configuration = await create_job_configuration(
        aci_credentials, worker_flow_run, job_overrides
    )
    # We haven't mocked out the container group creation, so this should fail
    # and that's ok. We just want to ensure the entrypoint is used correctly.
    with pytest.raises(RuntimeError):
        await aci_worker.run(worker_flow_run, run_job_configuration)

    mock_deployment_call.assert_called_once()
    (_, kwargs) = mock_deployment_call.call_args

    deployment_parameters = kwargs.get("parameters").properties
    called_command = get_command_from_deployment_parameters(deployment_parameters)
    assert called_command == expected_template_command


async def test_delete_after_group_creation_failure(
    aci_worker, worker_flow_run, job_configuration, mock_aci_client, monkeypatch
):
    # if provisioning failed, the container group should be deleted
    mock_container_group = Mock()
    mock_container_group.provisioning_state.return_value = (
        ContainerGroupProvisioningState.FAILED
    )

    monkeypatch.setattr(
        aci_worker, "_wait_for_task_container_start", mock_container_group
    )

    with pytest.raises(RuntimeError):
        await aci_worker.run(worker_flow_run, configuration=job_configuration)

    mock_aci_client.container_groups.begin_delete.assert_called_once()


async def test_delete_after_group_creation_success(
    aci_worker,
    worker_flow_run,
    job_configuration,
    mock_aci_client,
    monkeypatch,
    running_worker_container_group,
):
    # if provisioning was successful, the container group should eventually be deleted
    monkeypatch.setattr(
        aci_worker,
        "_wait_for_task_container_start",
        Mock(return_value=running_worker_container_group),
    )

    await aci_worker.run(worker_flow_run, job_configuration)
    mock_aci_client.container_groups.begin_delete.assert_called_once()


async def test_delete_after_after_exception(
    aci_worker,
    worker_flow_run,
    job_configuration,
    mock_aci_client,
    mock_resource_client,
    monkeypatch,
):
    # If an exception was thrown while waiting for container group provisioning,
    # we should still attempt to delete the container group. This is to ensure
    # that we don't leave orphaned container groups in the event of an error.
    mock_resource_client.deployments.begin_create_or_update.side_effect = (
        HttpResponseError(message="it broke")
    )

    with pytest.raises(HttpResponseError):
        await aci_worker.run(worker_flow_run, job_configuration)

    mock_aci_client.container_groups.begin_delete.assert_called_once()


@pytest.mark.usefixtures("mock_aci_client")
async def test_task_status_started_on_provisioning_success(
    aci_worker,
    worker_flow_run,
    job_configuration,
    running_worker_container_group,
    mock_prefect_client,
    monkeypatch,
):
    monkeypatch.setattr(aci_worker, "_provisioning_succeeded", Mock(return_value=True))

    monkeypatch.setattr(
        aci_worker,
        "_wait_for_task_container_start",
        Mock(return_value=running_worker_container_group),
    )

    task_status = Mock(spec=TaskStatus)
    await aci_worker.run(worker_flow_run, job_configuration, task_status=task_status)

    flow = await mock_prefect_client.read_flow(worker_flow_run.flow_id)

    container_group_name = f"{flow.name}-{worker_flow_run.id}"

    identifier = f"{worker_flow_run.id}:{container_group_name}"

    task_status.started.assert_called_once_with(value=identifier)


@pytest.mark.usefixtures("mock_aci_client")
async def test_task_status_not_started_on_provisioning_failure(
    aci_worker, worker_flow_run, job_configuration, monkeypatch
):
    monkeypatch.setattr(aci_worker, "_provisioning_succeeded", Mock(return_value=False))

    task_status = Mock(spec=TaskStatus)
    with pytest.raises(RuntimeError, match="Container creation failed"):
        await aci_worker.run(worker_flow_run, job_configuration, task_status)
    task_status.started.assert_not_called()


async def test_provisioning_timeout_throws_exception(
    aci_worker,
    worker_flow_run,
    job_configuration,
    mock_aci_client,
    mock_resource_client,
):
    mock_poller = Mock()
    mock_poller.done.return_value = False
    mock_resource_client.deployments.begin_create_or_update.side_effect = (
        lambda *args, **kwargs: mock_poller
    )

    # avoid delaying test runs
    job_configuration.task_watch_poll_interval = 0.09
    job_configuration.task_start_timeout_seconds = 0.10

    with pytest.raises(RuntimeError, match="Timed out after"):
        await aci_worker.run(worker_flow_run, job_configuration)


async def test_watch_for_container_termination(
    aci_worker,
    worker_flow_run,
    job_configuration,
    mock_aci_client,
    mock_resource_client,
    monkeypatch,
    running_worker_container_group,
    completed_worker_container_group,
):
    monkeypatch.setattr(aci_worker, "_provisioning_succeeded", Mock(return_value=True))

    monkeypatch.setattr(
        aci_worker,
        "_wait_for_task_container_start",
        Mock(return_value=running_worker_container_group),
    )

    # make the worker wait a few times before we give it a successful result
    # so we can make sure the watcher actually watches instead of skipping
    # the timeout
    run_count = 0

    def get_container_group(**kwargs):
        nonlocal run_count
        run_count += 1
        if run_count < 5:
            return running_worker_container_group
        else:
            return completed_worker_container_group

    mock_aci_client.container_groups.get.side_effect = get_container_group

    job_configuration.task_watch_poll_interval = 0.02
    result = await aci_worker.run(worker_flow_run, job_configuration)

    # ensure the watcher was watching
    assert run_count == 5
    assert mock_aci_client.container_groups.get.call_count == run_count
    # ensure the run completed
    assert isinstance(result, AzureContainerWorkerResult)


async def test_quick_termination_handling(
    aci_worker,
    worker_flow_run,
    job_configuration,
    mock_aci_client,
    completed_worker_container_group,
    monkeypatch,
):
    # ensure that everything works as expected in the case where the container has
    # already finished its flow run by the time the poller picked up the container
    # group's successful provisioning status.

    monkeypatch.setattr(aci_worker, "_provisioning_succeeded", Mock(return_value=True))

    monkeypatch.setattr(
        aci_worker,
        "_wait_for_task_container_start",
        Mock(return_value=completed_worker_container_group),
    )

    result = await aci_worker.run(worker_flow_run, job_configuration)

    # ensure the watcher didn't need to call to check status since the run
    # already completed.
    mock_aci_client.container_groups.get.assert_not_called()
    # ensure the run completed
    assert isinstance(result, AzureContainerWorkerResult)


async def test_output_streaming(
    aci_worker,
    worker_flow_run,
    job_configuration,
    mock_aci_client,
    running_worker_container_group,
    completed_worker_container_group,
    monkeypatch,
):
    # override datetime.now to ensure run start time is before log line timestamps
    run_start_time = dateutil.parser.parse("2022-10-03T20:40:05.3119525Z")
    mock_datetime = Mock()
    mock_datetime.datetime.now.return_value = run_start_time

    monkeypatch.setattr(prefect_azure.container_instance, "datetime", mock_datetime)

    log_lines = """
2022-10-03T20:41:05.3119525Z 20:41:05.307 | INFO    | Flow run 'ultramarine-dugong' - Created task run "Test-39fdf8ff-0" for task "ACI Test"
2022-10-03T20:41:05.3120697Z 20:41:05.308 | INFO    | Flow run 'ultramarine-dugong' - Executing "Test-39fdf8ff-0" immediately...
2022-10-03T20:41:05.6215928Z 20:41:05.616 | INFO    | Task run "Test-39fdf8ff-0" - Test Message
2022-10-03T20:41:05.7758864Z 20:41:05.775 | INFO    | Task run "Test-39fdf8ff-0" - Finished in state Completed()
"""  # noqa

    # include some overlap in the second batch so we can make sure output
    # is not duplicated
    next_log_lines = """
2022-10-03T20:41:05.6215928Z 20:41:05.616 | INFO    | Task run "Test-39fdf8ff-0" - Test Message
2022-10-03T20:41:05.7758864Z 20:41:05.775 | INFO    | Task run "Test-39fdf8ff-0" - Finished in state Completed()
2022-10-03T20:41:13.0149593Z 20:41:13.012 | INFO    | Flow run 'ultramarine-dugong' - Created task run "Test-39fdf8ff-1" for task "ACI Test"
2022-10-03T20:41:13.0152433Z 20:41:13.013 | INFO    | Flow run 'ultramarine-dugong' - Executing "Test-39fdf8ff-1" immediately...
2022-broken-03T20:41:13.0152433Z 20:41:13.013 | INFO    | Log line with broken timestamp should not be printed
    """  # noqa

    log_count = 0

    def get_logs(*args, **kwargs):
        nonlocal log_count
        logs = Mock()
        if log_count == 0:
            log_count += 1
            logs.content = log_lines
        elif log_count == 1:
            log_count += 1
            logs.content = next_log_lines
        else:
            logs.content = ""

        return logs

    run_count = 0

    def get_container_group(**kwargs):
        nonlocal run_count
        run_count += 1
        if run_count < 4:
            return running_worker_container_group
        else:
            return completed_worker_container_group

    mock_aci_client.container_groups.get.side_effect = get_container_group

    mock_log_call = Mock(side_effect=get_logs)
    monkeypatch.setattr(mock_aci_client.containers, "list_logs", mock_log_call)

    mock_write_call = Mock(wraps=aci_worker._write_output_line)
    monkeypatch.setattr(aci_worker, "_write_output_line", mock_write_call)

    monkeypatch.setattr(aci_worker, "_provisioning_succeeded", Mock(return_value=True))

    monkeypatch.setattr(
        aci_worker,
        "_wait_for_task_container_start",
        Mock(return_value=running_worker_container_group),
    )

    job_configuration.stream_output = True
    job_configuration.name = "streaming test"
    job_configuration.task_watch_poll_interval = 0.02
    await aci_worker.run(worker_flow_run, job_configuration)

    # 6 lines should be written because of the nine test log lines, two overlap
    # and should not be written twice, and one has a broken timestamp so should
    # not be written
    assert mock_write_call.call_count == 6


def test_block_accessible_in_module_toplevel():
    # will raise an exception and fail the test if `AzureContainerInstanceJob`
    # is not accessible directly from `prefect_azure`
    from prefect_azure import AzureContainerWorker  # noqa


def test_registry_credentials(aci_worker, mock_aci_client, monkeypatch):
    mock_container_group_constructor = MagicMock()

    monkeypatch.setattr(
        prefect_azure.container_instance,
        "ContainerGroup",
        mock_container_group_constructor,
    )

    registry = DockerRegistry(
        username="username",
        password="password",
        registry_url="https://myregistry.dockerhub.com",
    )

    job_configuration.image_registry = registry
    aci_worker.run(worker_flow_run, job_configuration)

    mock_container_group_constructor.assert_called_once()

    (_, kwargs) = mock_container_group_constructor.call_args
    registry_arg: List[ImageRegistryCredential] = kwargs.get(
        "image_registry_credentials"
    )

    # ensure the registry was used, passed as a list the way the Azure SDK expects it,
    # and correctly converted to an Azure ImageRegistryCredential.
    assert registry_arg is not None
    assert isinstance(registry_arg, list)

    registry_object = registry_arg[0]
    assert isinstance(registry_object, ImageRegistryCredential)
    assert registry_object.username == registry.username
    assert registry_object.password == registry.password.get_secret_value()
    assert registry_object.server == registry.registry_url
