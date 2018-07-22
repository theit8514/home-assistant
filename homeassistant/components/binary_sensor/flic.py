"""
Support to use flic buttons as a binary sensor.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/binary_sensor.flic/
"""
import logging
import threading
import time

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.const import (
    CONF_HOST, CONF_PORT, CONF_DISCOVERY, CONF_TIMEOUT,
    EVENT_HOMEASSISTANT_STOP)
from homeassistant.components.binary_sensor import (
    BinarySensorDevice, PLATFORM_SCHEMA)

REQUIREMENTS = ['pyflic-homeassistant==0.4.dev0']

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 3

DATA_FLIC = 'data_flic'

CLICK_TYPE_SINGLE = 'single'
CLICK_TYPE_DOUBLE = 'double'
CLICK_TYPE_HOLD = 'hold'
CLICK_TYPES = [CLICK_TYPE_SINGLE, CLICK_TYPE_DOUBLE, CLICK_TYPE_HOLD]

CONF_IGNORED_CLICK_TYPES = 'ignored_click_types'

DEFAULT_HOST = 'localhost'
DEFAULT_PORT = 5551

EVENT_NAME = 'flic_click'
EVENT_DATA_NAME = 'button_name'
EVENT_DATA_ADDRESS = 'button_address'
EVENT_DATA_TYPE = 'click_type'
EVENT_DATA_QUEUED_TIME = 'queued_time'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_HOST, default=DEFAULT_HOST): cv.string,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    vol.Optional(CONF_DISCOVERY, default=True): cv.boolean,
    vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
    vol.Optional(CONF_IGNORED_CLICK_TYPES):
        vol.All(cv.ensure_list, [vol.In(CLICK_TYPES)])
})

def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the flic platform."""
    hass.data[DATA_FLIC] = []
    client = connect_client(config)
    if client is None:
        _LOGGER.error("Failed to connect to flic server")
        return

    setup_client(client, hass, config, add_entities, discovery_info)
    def get_info_callback(items):
        """Add entities for already verified buttons."""
        addresses = items['bd_addr_of_verified_buttons'] or []
        for address in addresses:
            setup_button(hass, config, add_entities, client, address)

    # Get addresses of already verified buttons
    client.get_info(get_info_callback)

def handle_events(client, hass, config, add_entities, discovery_info):
    client.handle_events()
    entities = hass.data[DATA_FLIC]
    # when handle_events exits, the socket has been closed
    # this should be replaced by an event in the library
    _LOGGER.warning("Socket closed to flic server")
    for entity in entities:
        entity.disconnect()
    success = False

    while True:
        time.sleep(5)
        _LOGGER.info("Attempting connection to flic server")
        client = connect_client(config)
        if client is not None:
            break

    # at this phase, we set the client up again with add_entities, but we don't
    # add any entities. we find the buttons already set up and connect them to
    # the new client.
    setup_client(client, hass, config, add_entities, discovery_info)
    _LOGGER.info("Reconnected to flic server... reconnecting buttons to client")
    for entity in entities:
        entity.connect(client)

def connect_client(config):
    import pyflic
    # Initialize flic client responsible for
    # connecting to buttons and retrieving events
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT)
    try:
        client = pyflic.FlicClient(host, port)
    except:
        _LOGGER.exception("Failed to connect to flic server")
        return None

    return client

def setup_client(client, hass, config, add_entities, discovery_info):
    """Configures the client callbacks and registers hass events"""
    def new_button_callback(address):
        """Set up newly verified button as device in Home Assistant."""
        button = setup_button(hass, config, add_entities, client, address)

    client.on_new_verified_button = new_button_callback
    discovery = config.get(CONF_DISCOVERY)
    if discovery:
        start_scanning(config, add_entities, client)

    hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP,
                         lambda event: client.close())

    # Start the pyflic event handling thread
    threading.Thread(target=handle_events, args=[client, hass, config, add_entities, discovery_info]).start()

def start_scanning(config, add_entities, client):
    """Start a new flic client for scanning and connecting to new buttons."""
    import pyflic

    scan_wizard = pyflic.ScanWizard()

    def scan_completed_callback(scan_wizard, result, address, name):
        """Restart scan wizard to constantly check for new buttons."""
        if result == pyflic.ScanWizardResult.WizardSuccess:
            _LOGGER.info("Found new button %s", address)
        elif result != pyflic.ScanWizardResult.WizardFailedTimeout:
            _LOGGER.warning(
                "Failed to connect to button %s. Reason: %s", address, result)

        # Restart scan wizard
        start_scanning(config, add_entities, client)

    scan_wizard.on_completed = scan_completed_callback
    client.add_scan_wizard(scan_wizard)


def setup_button(hass, config, add_entities, client, address):
    """Set up a single button device."""
    timeout = config.get(CONF_TIMEOUT)
    ignored_click_types = config.get(CONF_IGNORED_CLICK_TYPES)
    button = FlicButton(hass, address, timeout, ignored_click_types)
    button.connect(client)
    _LOGGER.info("Connected to button %s", address)
    entities = hass.data[DATA_FLIC]
    entities.append(button)

    add_entities([button])


class FlicButton(BinarySensorDevice):
    """Representation of a flic button."""

    def __init__(self, hass, address, timeout, ignored_click_types):
        """Initialize the flic button."""
        import pyflic

        self._hass = hass
        self._address = address
        self._timeout = timeout
        self._is_down = False
        self._ignored_click_types = ignored_click_types or []
        self._hass_click_types = {
            pyflic.ClickType.ButtonClick: CLICK_TYPE_SINGLE,
            pyflic.ClickType.ButtonSingleClick: CLICK_TYPE_SINGLE,
            pyflic.ClickType.ButtonDoubleClick: CLICK_TYPE_DOUBLE,
            pyflic.ClickType.ButtonHold: CLICK_TYPE_HOLD,
        }

        self._is_connected = False
        self._channel = self._create_channel()

    def _create_channel(self):
        """Create a new connection channel to the button."""
        import pyflic

        channel = pyflic.ButtonConnectionChannel(self._address)
        channel.on_button_up_or_down = self._on_up_down

        # If all types of clicks should be ignored, skip registering callbacks
        if set(self._ignored_click_types) == set(CLICK_TYPES):
            return channel

        if CLICK_TYPE_DOUBLE in self._ignored_click_types:
            # Listen to all but double click type events
            channel.on_button_click_or_hold = self._on_click
        elif CLICK_TYPE_HOLD in self._ignored_click_types:
            # Listen to all but hold click type events
            channel.on_button_single_or_double_click = self._on_click
        else:
            # Listen to all click type events
            channel.on_button_single_or_double_click_or_hold = self._on_click

        return channel

    def disconnect(self):
        self._is_connected = False

    def connect(self, client):
        self._is_connected = True
        client.add_connection_channel(self._channel)

    @property
    def available(self):
        return self._is_connected

    @property
    def name(self):
        """Return the name of the device."""
        return 'flic_{}'.format(self.address.replace(':', ''))

    @property
    def address(self):
        """Return the bluetooth address of the device."""
        return self._address

    @property
    def is_on(self):
        """Return true if sensor is on."""
        return self._is_down

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        return {'address': self.address}

    def _queued_event_check(self, click_type, time_diff):
        """Generate a log message and returns true if timeout exceeded."""
        time_string = "{:d} {}".format(
            time_diff, 'second' if time_diff == 1 else 'seconds')

        if time_diff > self._timeout:
            _LOGGER.warning(
                "Queued %s dropped for %s. Time in queue was %s",
                click_type, self.address, time_string)
            return True
        _LOGGER.info(
            "Queued %s allowed for %s. Time in queue was %s",
            click_type, self.address, time_string)
        return False

    def _on_up_down(self, channel, click_type, was_queued, time_diff):
        """Update device state, if event was not queued."""
        import pyflic

        if was_queued and self._queued_event_check(click_type, time_diff):
            return

        self._is_down = click_type == pyflic.ClickType.ButtonDown
        self.schedule_update_ha_state()

    def _on_click(self, channel, click_type, was_queued, time_diff):
        """Fire click event, if event was not queued."""
        # Return if click event was queued beyond allowed timeout
        if was_queued and self._queued_event_check(click_type, time_diff):
            return

        # Return if click event is in ignored click types
        hass_click_type = self._hass_click_types[click_type]
        if hass_click_type in self._ignored_click_types:
            return

        self._hass.bus.fire(EVENT_NAME, {
            EVENT_DATA_NAME: self.name,
            EVENT_DATA_ADDRESS: self.address,
            EVENT_DATA_QUEUED_TIME: time_diff,
            EVENT_DATA_TYPE: hass_click_type
        })

    def _connection_status_changed(
            self, channel, connection_status, disconnect_reason):
        """Remove device, if button disconnects."""
        import pyflic

        if connection_status == pyflic.ConnectionStatus.Disconnected:
            _LOGGER.warning("Button (%s) disconnected. Reason: %s",
                            self.address, disconnect_reason)
