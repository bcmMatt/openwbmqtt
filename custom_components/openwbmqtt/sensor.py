"""The openwbmqtt component for controlling the openWB wallbox via home assistant MQTT."""
from __future__ import annotations

import copy
from datetime import timedelta
import logging
import re

from homeassistant.components import mqtt
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import async_get as async_get_dev_reg
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from .common import OpenWBBaseEntity

# Import global values.
from .const import (
    CHARGEPOINTS,
    MQTTROOTTOPIC,
    SENSORSGLOBAL,
    SENSORSPERLP,
    openwbSensorEntityDescription,
)

LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for openWB."""
    integrationUniqueID = config.unique_id
    mqttRoot = config.data[MQTTROOTTOPIC]
    nChargePoints = config.data[CHARGEPOINTS]

    sensorList: list[openwbSensor] = []

    # Create all global sensors.
    globalsensors = copy.deepcopy(SENSORSGLOBAL)
    for description in globalsensors:
        description.mqttTopicCurrentValue = f"{mqttRoot}{description.key}"
        LOGGER.debug("mqttTopic %s", description.mqttTopicCurrentValue)
        sensorList.append(
            openwbSensor(
                uniqueID=integrationUniqueID,
                description=description,
                devicefriendlyname=integrationUniqueID,
                mqttroot=mqttRoot,
            )
        )

    # Create all sensors for each charge point, respectively.
    for chargePoint in range(1, nChargePoints + 1):
        localsensorsperlp = copy.deepcopy(SENSORSPERLP)
        for description in localsensorsperlp:
            description.mqttTopicCurrentValue = (
                f"{mqttRoot}lp{str(chargePoint)}{description.key}"
            )
            LOGGER.debug("mqttTopic %s", description.mqttTopicCurrentValue)
            sensorList.append(
                openwbSensor(
                    uniqueID=integrationUniqueID,
                    description=description,
                    nChargePoints=int(nChargePoints),
                    currentChargePoint=chargePoint,
                    devicefriendlyname=integrationUniqueID,
                    mqttroot=mqttRoot,
                )
            )

    async_add_entities(sensorList)


class openwbSensor(OpenWBBaseEntity, SensorEntity):
    """Representation of an openWB sensor that is updated via MQTT."""

    entity_description: openwbSensorEntityDescription

    def __init__(
        self,
        uniqueID: str | None = None,
        devicefriendlyname: str = "",
        mqttroot: str = "",
        description: openwbSensorEntityDescription = openwbSensorEntityDescription,
        nChargePoints: int | None = None,
        currentChargePoint: int | None = None,
    ) -> None:
        """Initialize the sensor and the openWB device."""
        super().__init__(devicefriendlyname=devicefriendlyname, mqttroot=mqttroot)

        self.entitydescription = description

        if nChargePoints:
            # Nur unique_id und name setzen, entity_id wird von HA generiert
            self._attr_unique_id = slugify(
                f"{uniqueID}-CP{currentChargePoint}-{description.name}"
            )
            self._attr_name = f"{description.name} LP{currentChargePoint}"
        else:
            self._attr_unique_id = slugify(f"{uniqueID}-{description.name}")
            self._attr_name = description.name

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT events."""
        await super().async_added_to_hass()

        @callback
        def message_received(message):
            """Handle new MQTT messages."""
            self._attr_native_value = message.payload

            # Convert data if a conversion function is defined
            if self.entitydescription.value_fn is not None:
                self._attr_native_value = self.entitydescription.value_fn(
                    self._attr_native_value
                )

            # Map values as defined in the value map dict.
            if self.entitydescription.value_map is not None:
                try:
                    self._attr_native_value = self.entitydescription.value_map.get(
                        int(self._attr_native_value)
                    )
                except ValueError:
                    self._attr_native_value = self._attr_native_value

            # Reformat TimeRemaining -> timestamp.
            if "TimeRemaining" in self.entitydescription.key:
                now = dt_util.utcnow()
                if "H" in self._attr_native_value:
                    tmp = self._attr_native_value.split(":")
                    delta = timedelta(hours=int(tmp[0]), minutes=int(tmp[1]))
                    self._attr_native_value = now + delta
                elif "Min" in self._attr_native_value:
                    tmp = self._attr_native_value.split(":")
                    delta = timedelta(minutes=int(tmp[0]))
                    self._attr_native_value = now + delta
                else:
                    self._attr_native_value = None

            # Reformat uptime sensor
            if "uptime" in self.entity_id:
                reluptime = re.match(r"^(.+?\.)?(\d+)([hdm])(\.|$).*", self._attr_native_value, re.IGNORECASE)
                days = 0
                if re.match(r"^\d+d", reluptime.group(1), re.IGNORECASE):
                    days = re.match(r"^(\d+)d", reluptime.group(1), re.IGNORECASE).group(1)
                if re.match(r"\d+h", reluptime.group(2), re.IGNORECASE):
                    hours = 0
                    mins = re.match(r"(\d+)h", reluptime.group(2), re.IGNORECASE).group(1)
                else:
                    hours, mins = re.match(r"?(\d+):?0?(\d+)", reluptime.group(2)).group(1, 2)
                self._attr_native_value = f"{days}d {hours}h {mins}min"

            # If MQTT message contains IP -> set up configurationurl to visit the device
            elif "ipadresse" in self.entity_id:
                device_registry = async_get_dev_reg(self.hass)
                device = device_registry.async_get_device(self.device_info.get("identifiers", []))
                if device:
                    device_registry.async_update_device(device.id, {"configuration_url": f"http://{message.payload}/openWB/web/index.php"})

            # If MQTT message contains version -> set swversion of the device
            elif "version" in self.entity_id:
                device_registry = async_get_dev_reg(self.hass)
                device = device_registry.async_get_device(self.device_info.get("identifiers", []))
                if device:
                    device_registry.async_update_device(device.id, {"sw_version": message.payload})

            # Update icon of countPhasesInUse
            elif "countPhasesInUse" in self.entitydescription.key:
                if int(message.payload) == 0:
                    self._attr_icon = "mdi:numeric-0-circle-outline"
                elif int(message.payload) == 1:
                    self._attr_icon = "mdi:numeric-1-circle-outline"
                elif int(message.payload) == 3:
                    self._attr_icon = "mdi:numeric-3-circle-outline"
                else:
                    self._attr_icon = "mdi:numeric"

            # Update entity state with value published on MQTT.
            self.async_write_ha_state()

        # Subscribe to MQTT topic and connect callack message
        await mqtt.async_subscribe(
            self.hass,
            self.entitydescription.mqttTopicCurrentValue,
            message_received,
            1,
        )
