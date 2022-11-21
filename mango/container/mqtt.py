import asyncio
import logging, warnings
from typing import Optional, Union, Tuple, Dict, Any, Set

import paho.mqtt.client as paho

from ..messages.codecs import Codec, ACLMessage
from ..util.clock import Clock
from mango.messages.codecs import JSON
from mango.container.core import Container

logger = logging.getLogger(__name__)

class MQTTContainer(Container):
    """
    Container for agents.

       The container allows its agents to send messages to specific topics
       (via :meth:`send_message()`).
    """

    def __init__(
        self,
        *,
        client_id: str,
        addr: Optional[str],
        loop: asyncio.AbstractEventLoop,
        clock: Clock,
        mqtt_client: paho.Client,
        codec: Codec = JSON,
        proto_msgs_module=None,
    ):
        """
        Initializes a container. Do not directly call this method but use
        the factory method instead
        :param client_id: The ID that the container should use when connecting
        to the broker
        :param addr: A string of the unique inbox topic to use.
        No wildcards are allowed. If None, no inbox topic will be set
        :param mqtt_client: The paho.Client object that is used for the
        communication with the broker
        :param codec: The codec to use. Currently only 'json' or 'protobuf' are
         allowed
        :param proto_msgs_module: The compiled python module where the
         additional proto msgs are defined
        """
        super().__init__(
            codec=codec,
            addr=addr,
            proto_msgs_module=proto_msgs_module,
            loop=loop,
            clock=clock,
            name=client_id,
        )

        self.client_id: str = client_id
        # the configured and connected paho client
        self.mqtt_client: paho.Client = mqtt_client
        self.inbox_topic: Optional[str] = addr
        # dict mapping additionally subscribed topics to a set of aids
        self.additional_subscriptions: Dict[str, Set[str]] = {}
        # dict mapping subscribed topics to the expected class
        self.subscriptions_to_class: Dict[str, Any] = {}
        # Future for pending sub requests
        self.pending_sub_request: Optional[asyncio.Future] = None

        # set the callbacks
        self._set_mqtt_callbacks()

        # start the mqtt client
        self.mqtt_client.loop_start()

    def _set_mqtt_callbacks(self):
        """
        Sets the callbacks for the mqtt paho client
        """

        def on_con(client, userdata, flags, rc):
            if rc != 0:
                logger.info("Connection attempt to broker failed")
            else:
                logger.debug("Successfully reconnected to broker.")

        self.mqtt_client.on_connect = on_con

        def on_discon(client, userdata, rc):
            if rc != 0:
                logger.warning("Unexpected disconnect from broker. Trying to reconnect")
            else:
                logger.debug("Successfully disconnected from broker.")

        self.mqtt_client.on_disconnect = on_discon

        def on_sub(client, userdata, mid, granted_qos):
            self.loop.call_soon_threadsafe(self.pending_sub_request.set_result, 0)

        self.mqtt_client.on_subscribe = on_sub

        def on_msg(client, userdata, message):
            # extract the meta information first
            meta = {
                "network_protocol": "mqtt",
                "topic": message.topic,
                "qos": message.qos,
                "retain": message.retain,
            }
            # decode message and extract msg_content and meta
            msg_content, msg_meta = self.decode_mqtt_msg(
                payload=message.payload, topic=message.topic
            )
            # update meta dict
            meta.update(msg_meta)

            # put information to inbox
            if msg_content is not None:
                self.loop.call_soon_threadsafe(
                    self.inbox.put_nowait, (0, msg_content, meta)
                )

        self.mqtt_client.on_message = on_msg

        self.mqtt_client.enable_logger(logger)

    async def shutdown(self):
        """
        Shutdown container, disconnect from broker and stop mqtt thread
        """
        await super().shutdown()
        # disconnect to broker
        self.mqtt_client.disconnect()
        self.mqtt_client.loop_stop()

    def decode_mqtt_msg(self, *, topic, payload):
        """
        deserializes a mqtt msg.
        Checks if for the topic a special class is defined, otherwise assumes
        an ACLMessage
        :param topic: the topic on which the message arrived
        :param payload: the serialized message
        :return: content and meta
        """
        meta = {}
        content = None

        # check if there is a class definition for the topic
        for sub, sub_class in self.subscriptions_to_class.items():
            if paho.topic_matches_sub(sub, topic):
                # instantiate the provided class
                content = sub_class()
                break

        decoded = self.codec.decode(payload)
        if isinstance(content, ACLMessage):
            meta = decoded.extract_meta()
            content = decoded.content

        return decoded, meta

    async def _handle_msg(self, *, priority: int, msg_content, meta: Dict[str, Any]):
        """
        This is called as a separate task for every message that is read
        :param priority: priority of the msg
        :param msg_content: Deserialized content of the message
        :param meta: Dict with additional information (e.g. topic)

        """
        topic = meta["topic"]
        logger.debug(
            f"Received msg with content and meta;{str(msg_content)};{str(meta)}"
        )

        if hasattr(msg_content, "split_content_and_meta"):
            content, msg_meta = msg_content.split_content_and_meta()
            meta.update(msg_meta)
            msg_content = content

        if topic == self.inbox_topic:
            # General inbox topic, so no receiver is specified by the topic
            # try to find the receiver from meta
            receiver_id = meta.get("receiver_id", None)
            if receiver_id and receiver_id in self._agents.keys():
                receiver = self._agents[receiver_id]
                await receiver.inbox.put((priority, msg_content, meta))
            else:
                logger.warning(f"Receiver ID is unknown;{receiver_id}")
        else:
            # no inbox topic. Check who has subscribed the topic.
            receivers = set()
            for sub, rec in self.additional_subscriptions.items():
                if paho.topic_matches_sub(sub, topic):
                    receivers.update(rec)
            if not receivers:
                logger.warning(
                    f"Received a message at a topic which no agent subscribed;{topic}"
                )
            else:
                for receiver_id in receivers:
                    receiver = self._agents[receiver_id]

                    await receiver.inbox.put((priority, msg_content, meta))

    async def send_message(
        self,
        content,
        receiver_addr: Union[str, Tuple[str, int]],
        *,
        receiver_id: Optional[str] = None,
        create_acl: bool = None,
        acl_metadata: Optional[Dict[str, Any]] = None,
        mqtt_kwargs: Dict[str, Any] = None,
        **kwargs
    ):
        """
        The container sends the message of one of its own agents to a specific topic.
        
        :param content: The content of the message
        :param receiver_addr: The topic to publish to.
        :param receiver_id: The agent id of the receiver
        :param create_acl: True if an acl message shall be created around the
            content.
            
            .. deprecated:: 0.4.0
                Use 'container.send_acl_message' instead. In the next version this parameter
                will be dropped entirely.
        :param acl_metadata: metadata for the acl_header.
            Ignored if create_acl == False
            
            .. deprecated:: 0.4.0
                Use 'container.send_acl_message' instead. In the next version this parameter
                will be dropped entirely.
        :param mqtt_kwargs: Dict with possible kwargs for publishing to a mqtt broker
            Possible fields:
            qos: The quality of service to use for publishing
            retain: Indicates, weather the retain flag should be set
            Ignored if connection_type != 'mqtt'
            .. deprecated:: 0.4.0
                Use 'kwargs' instead. In the next version this parameter
                will be dropped entirely.
        :param kwargs: Additional parameters to provide protocol specific settings 
            Possible fields:
            qos: The quality of service to use for publishing
            retain: Indicates, weather the retain flag should be set
            Ignored if connection_type != 'mqtt'

        """

        if create_acl is not None or acl_metadata is not None:
            warnings.warn("The parameters create_acl and acl_metadata are deprecated and will " \
                          "be removed in the next release. Use send_acl_message instead.", DeprecationWarning)
        if mqtt_kwargs is not None:
            warnings.warn("The parameter mqtt_kwargs is deprecated and will " \
                          "be removed in the next release. Use kwargs instead.", DeprecationWarning)

        if create_acl:
            message = self._create_acl(
                content=content,
                receiver_addr=receiver_addr,
                receiver_id=receiver_id,
                acl_metadata=acl_metadata,
            )
        else:
            # the message is already complete
            message = content

        # internal message first (if retain Flag is set, it has to be sent to
        # the broker
        actual_mqtt_kwargs = mqtt_kwargs if kwargs is None else kwargs
        actual_mqtt_kwargs = {} if actual_mqtt_kwargs is None else actual_mqtt_kwargs
        if (
            self.addr
            and receiver_addr == self.addr
            and not actual_mqtt_kwargs.get("retain", False)
        ):
            meta = {
                "topic": self.addr,
                "qos": actual_mqtt_kwargs.get("qos", 0),
                "retain": False,
                "network_protocol": "mqtt",
            }

            if hasattr(message, "split_content_and_meta"):
                content, msg_meta = message.split_content_and_meta()
                meta.update(msg_meta)
            else:
                content = message

            self.inbox.put_nowait((0, content, meta))
            return True

        else:
            self._send_external_message(topic=receiver_addr, message=message)
            return True

    def _send_external_message(self, *, topic: str, message):
        """

        :param topic: MQTT topic
        :param message: The ACL message
        :return:
        """
        encoded_msg = self.codec.encode(message)
        # if self.codec == "json":
        #     encoded_msg = message.encode()
        # elif self.codec == "protobuf":
        #     encoded_msg = message.SerializeToString()
        # else:
        #     raise ValueError("Unknown codec")
        logger.debug(f"Sending message;{message};{topic}")
        self.mqtt_client.publish(topic, encoded_msg)

    async def subscribe_for_agent(
        self, *, aid: str, topic: str, qos: int = 0, expected_class=None
    ) -> bool:
        """

        :param aid: aid of the corresponding agent
        :param topic: topic to subscribe (wildcards are allowed)
        :param qos: The quality of service for the subscription
        :param expected_class: The class to expect from the topic, defaults
        to ACL
        :return: A boolean signaling if subscription was true or not
        """
        if aid not in self._agents.keys():
            raise ValueError("Given aid is not known")
        if expected_class:
            self.subscriptions_to_class[topic] = expected_class

        if topic in self.additional_subscriptions.keys():
            self.additional_subscriptions[topic].add(aid)
            return True

        self.additional_subscriptions[topic] = {aid}
        self.pending_sub_request = asyncio.Future()
        result, _ = self.mqtt_client.subscribe(topic, qos=qos)

        if result != paho.MQTT_ERR_SUCCESS:
            self.pending_sub_request.set_result(False)
            return False

        await self.pending_sub_request
        return True

    def set_expected_class(self, *, topic: str, expected_class):
        """
        Sets an expected class to a subscription
        wildcards are allowed here
        :param topic: The subscription
        :param expected_class: The expected class
        :return:
        """
        self.subscriptions_to_class[topic] = expected_class
        logger.debug(f"Expected class updated;{self.subscriptions_to_class}")

    def deregister_agent(self, aid):
        """

        :param aid:
        :return:
        """
        super().deregister_agent(aid)
        empty_subscriptions = []
        for subscription, aid_set in self.additional_subscriptions.items():
            if aid in aid_set:
                aid_set.remove(aid)
            if len(aid_set) == 0:
                empty_subscriptions.append(subscription)

        for subscription in empty_subscriptions:
            self.additional_subscriptions.pop(subscription)
            self.mqtt_client.unsubscribe(topic=subscription)

