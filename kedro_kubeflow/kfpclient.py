import logging
import uuid
from tempfile import NamedTemporaryFile

from kfp import Client
from kfp.compiler import Compiler
from tabulate import tabulate

from kedro_kubeflow.generators.one_pod_pipeline_generator import (
    OnePodPipelineGenerator,
)
from kedro_kubeflow.generators.pod_per_node_pipeline_generator import (
    PodPerNodePipelineGenerator,
)

from .auth import AuthHandler
from .utils import clean_name

WAIT_TIMEOUT = 24 * 60 * 60


class KubeflowClient(object):

    log = logging.getLogger(__name__)

    def __init__(self, config, project_name, context):
        client_params = {}
        token = AuthHandler().obtain_id_token()
        if token is not None:
            client_params = {"existing_token": token}
        dex_authservice_session = AuthHandler().obtain_dex_authservice_session(
            kfp_api=config.host,
        )
        if dex_authservice_session is not None:
            client_params = {
                "cookies": f"authservice_session={dex_authservice_session}"
            }
        self.host = config.host
        self.client = Client(host=self.host, **client_params)

        self.project_name = project_name
        self.pipeline_description = config.run_config.description
        if config.run_config.node_merge_strategy == "none":
            self.generator = PodPerNodePipelineGenerator(
                config, project_name, context
            )
        elif config.run_config.node_merge_strategy == "full":
            self.generator = OnePodPipelineGenerator(
                config, project_name, context
            )
        else:
            raise Exception(
                f"Invalid `node_merge_strategy`: {config.run_config.node_merge_strategy}"
            )

    def list_pipelines(self):
        pipelines = self.client.list_pipelines(page_size=30).pipelines
        return tabulate(
            map(lambda x: [x.name, x.id], pipelines), headers=["Name", "ID"]
        )

    def run_once(
        self,
        pipeline,
        image,
        experiment_name,
        experiment_namespace,
        run_name,
        wait,
        image_pull_policy="IfNotPresent",
        parameters={},
    ) -> None:
        run = self.client.create_run_from_pipeline_func(
            self.generator.generate_pipeline(
                pipeline, image, image_pull_policy
            ),
            arguments=parameters,
            experiment_name=experiment_name,
            namespace=experiment_namespace,
            run_name=run_name.format(**parameters),
        )

        if wait:
            run.wait_for_run_completion(timeout=WAIT_TIMEOUT)

    def compile(
        self, pipeline, image, output, image_pull_policy="IfNotPresent"
    ):
        Compiler().compile(
            self.generator.generate_pipeline(
                pipeline, image, image_pull_policy
            ),
            output,
        )
        self.log.info("Generated pipeline definition was saved to %s" % output)

    def get_full_pipeline_name(self, pipeline_name, env):
        return f"[{self.project_name}] {pipeline_name} (env: {env})"[:100]

    def upload(self, pipeline_name, image, image_pull_policy, env):
        pipeline = self.generator.generate_pipeline(
            pipeline_name, image, image_pull_policy
        )

        full_pipeline_name = self.get_full_pipeline_name(pipeline_name, env)
        if self._pipeline_exists(full_pipeline_name):
            pipeline_id = self.client.get_pipeline_id(full_pipeline_name)
            version_id = self._upload_pipeline_version(pipeline, pipeline_id)
            self.log.info("New version of pipeline created: %s", version_id)
        else:
            (pipeline_id, version_id) = self._upload_pipeline(
                pipeline, full_pipeline_name
            )
            self.log.info("Pipeline created")

        self.log.info(
            f"Pipeline link: {self.host}/#/pipelines/details/%s/version/%s",
            pipeline_id,
            version_id,
        )

    def _pipeline_exists(self, pipeline_name):
        return self.client.get_pipeline_id(pipeline_name) is not None

    def _upload_pipeline_version(self, pipeline_func, pipeline_id):
        version_name = f"{clean_name(self.project_name)}-{uuid.uuid4()}"[:100]
        with NamedTemporaryFile(suffix=".yaml") as f:
            Compiler().compile(pipeline_func, f.name)
            return self.client.pipeline_uploads.upload_pipeline_version(
                f.name,
                name=version_name,
                pipelineid=pipeline_id,
                _request_timeout=10000,
            ).id

    def _upload_pipeline(self, pipeline_func, pipeline_name):
        with NamedTemporaryFile(suffix=".yaml") as f:
            Compiler().compile(pipeline_func, f.name)
            pipeline = self.client.pipeline_uploads.upload_pipeline(
                f.name,
                name=pipeline_name,
                description=self.pipeline_description,
                _request_timeout=10000,
            )
            return (pipeline.id, pipeline.default_version.id)

    def _ensure_experiment_exists(self, experiment_name, experiment_namespace):
        try:
            experiment = self.client.get_experiment(
                experiment_name=experiment_name,
                namespace=experiment_namespace,
            )
            self.log.info(f"Existing experiment found: {experiment.id}")
        except ValueError as e:
            if not str(e).startswith("No experiment is found"):
                raise

            experiment = self.client.create_experiment(
                experiment_name, namespace=experiment_namespace
            )
            self.log.info(f"New experiment created: {experiment.id}")

        return experiment.id

    def schedule(
        self,
        pipeline,
        experiment_name,
        experiment_namespace,
        cron_expression,
        run_name,
        parameters,
        env,
    ):
        experiment_id = self._ensure_experiment_exists(
            experiment_name, experiment_namespace
        )
        pipeline_id = self.client.get_pipeline_id(
            self.get_full_pipeline_name(pipeline, env)
        )
        formatted_run_name = run_name.format(**parameters)
        self._disable_runs(experiment_id, formatted_run_name)
        self.client.create_recurring_run(
            experiment_id,
            formatted_run_name,
            cron_expression=cron_expression,
            pipeline_id=pipeline_id,
            params=parameters,
        )
        self.log.info("Pipeline scheduled to %s", cron_expression)

    def _disable_runs(self, experiment_id, run_name):
        runs = self.client.list_recurring_runs(experiment_id=experiment_id)
        if runs.jobs is None:
            return

        my_runs = [job for job in runs.jobs if job.name == run_name]
        for job in my_runs:
            self.client.jobs.delete_job(job.id)
            self.log.info(f"Previous schedule deleted {job.id}")
