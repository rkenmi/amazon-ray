import copy
import json
import logging
import time
from typing import Dict, Any
from abc import ABC, abstractmethod

from botocore.exceptions import ClientError

from ray.autoscaler._private.aws.sns.snsu import SnsHelper
from ray.autoscaler._private.cli_logger import cli_logger
from ray.autoscaler._private.event_system import CreateClusterEvent, ScriptStartedEvent, ScriptInProgressEvent, \
    ScriptCompletedEvent, RayEvent, event_enum_values, global_event_system, ScriptInProgressCustomEvent
from ray.autoscaler._private.event_publisher import EventPublisher
from ray.autoscaler._private.updater import NodeContext
from typing import Optional

logger = logging.getLogger(__name__)


class AwsEventManagerBase(ABC):
    uri: str
    parameters: Dict[str, any]

    def add_callback(self, event: RayEvent):
        """Adds a callback handler based on the ARN that is supplied in `events.notification_uri`.
        Currently, only SNS topics are supported.

        Args:
            event: The cluster event to invoke the callback handler for
        """
        if self.uri.startswith("arn:aws:sns"):
            global_event_system.add_callback_handler(event,
                                                     self._sns_callback,
                                                     SnsHelper(self._get_region()),
                                                     **self.parameters)
            logger.info("Added SNS callback handler for event %s", event.name)
        elif self.uri.startswith("arn:aws:lambda"):
            global_event_system.add_callback_handler(event, self._lambda_callback)
            logger.info("Added Lambda callback handler for event %s", event.name)
        elif self.uri.startswith("arn:aws:logs"):
            global_event_system.add_callback_handler(event, self._cloudwatch_callback)
            logger.info("Added Cloudwatch callback handler for event %s", event.name)
        elif self.uri.startswith("arn:aws:apigateway"):
            global_event_system.add_callback_handler(event, self._api_gateway_callback)
            logger.info("Added API Gateway callback handler for event %s", event.name)

    def publish(self, event: RayEvent, event_data: Optional[Dict[str, Any]] = None):
        global_event_system.execute_callback(event, event_data)

    def _get_region(self) -> str:
        return self.uri.split(":")[3]

    @property
    @abstractmethod
    def config(self) -> Dict[str, Any]:
        raise NotImplementedError("Configuration is not defined")

    @abstractmethod
    def _sns_callback(self, sns_client: SnsHelper, event_data: Dict[str, Any], **kwargs):
        raise NotImplementedError("SNS callback is not implemented")

    def _lambda_callback(self):
        raise NotImplementedError("AWS Lambda callback is not implemented")

    def _cloudwatch_callback(self):
        raise NotImplementedError("AWS Cloudwatch callback is not implemented")

    def _api_gateway_callback(self):
        raise NotImplementedError("AWS API Gateway callback is not implemented")


class AwsEventManager(AwsEventManagerBase):
    def __init__(self, events_config: Dict[str, Any]):
        self.events_config = events_config
        self.uri = notification_uri = events_config["notification_uri"]
        assert notification_uri is not None, f"`notification_uri` is a required field in `events`"
        assert "arn:aws" in notification_uri, f"Invalid ARN specified: {notification_uri}"
        self.parameters = events_config.get("parameters", {})

    @property
    def config(self) -> Dict[str, Any]:
        return self.events_config

    def _sns_callback(self, sns_client: SnsHelper, event_data: Dict[str, Any], **kwargs):
        """SNS callback for sending Ray cluster event data to an SNS topic.

        Args:
            sns_client: Amazon SNS client for publishing to an SNS topic
            event_data: Ray cluster setup event data. This contains the event name, enum ID, and
                may also contain additional metadata (i.e. the initialization or setup command used
                during this setup step)
            **kwargs: Keyword arguments injected into `_EventSystem.add_callback_handler` before initialization
        """
        try:
            # create a copy of the event data to modify
            event_dict = copy.deepcopy(event_data)
            event: RayEvent = event_dict.pop("event")
            node_context: NodeContext = event_dict.get("node_context", {})
            sns_topic_arn, params = self.uri, kwargs
            custom_description = event_dict.get("customDescription")
            custom_event_name = event_dict.get("customEventName")
            message = {
                **params,
                "state": event.state,
                "stateSequence": event.value - 1,  # zero-index sequencing
                "stateDetailStatus": "SUCCESS",
                "timestamp": round(time.time() * 1000),
            }

            if custom_event_name:
                message["eventName"] = custom_event_name

            if custom_description:
                message["stateDetailDescription"] = custom_description

            if node_context:
                message["rayNodeId"] = node_context["node_id"]
                message["rayNodeType"] = "HEAD" if node_context["is_head_node"] else "WORKER"

            sns_client.publish(sns_topic_arn, json.dumps(message))
            logger.info("Published SNS event {} to {}".format(
                event.name, sns_topic_arn))
        except ClientError as exc:
            cli_logger.abort(
                "{} Error caught when publishing {} create cluster events to SNS",
                exc.response["Error"], event.name)
