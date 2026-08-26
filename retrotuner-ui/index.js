'use strict';

var libQ = require('kew');
var fs = require('fs-extra');
var exec = require('child_process').exec;

// Dropped just before a self-triggered restart so the python service can tell a
// restart (capture/settings save) apart from a genuine stop/shutdown.
var RESTART_MARKER_PATH = '/tmp/retrotuner-ui-restarting';

// SPI mode does nothing without this line enabling the kernel driver. Volumio's
// node process can write this file directly -- gpio_button_led does the same --
// so the line is added rather than merely reported, and the user is asked to
// reboot, which is the only thing that loads the driver.
var USERCONFIG_PATH = '/boot/userconfig.txt';
var SPI_USERCONFIG_LINE = 'dtparam=spi=on';
var SPI_USERCONFIG_COMMENT =
    '# Added by RetroTuner UI: the MCP3008 button ADC needs the SPI kernel driver';

function isSpiEnabledInUserConfig() {
    try {
        var contents = fs.readFileSync(USERCONFIG_PATH, 'utf8');
        return /^\s*dtparam\s*=\s*spi\s*=\s*on\s*$/m.test(contents);
    } catch (e) {
        return false;
    }
}

// Ensure dtparam=spi=on is present. Returns 'present', 'added', or an error
// string. Only a reboot actually loads the driver, so 'added' means the caller
// must ask for one -- until then every MCP3008 read comes back as 0.
retrotunerui.prototype.ensureSpiInUserConfig = function () {
    const self = this;
    if (isSpiEnabledInUserConfig()) { return 'present'; }

    let contents = '';
    try {
        contents = fs.readFileSync(USERCONFIG_PATH, 'utf8');
    } catch (e) {
        contents = '';   // absent is fine -- writing creates it
    }

    // A file with no trailing newline would otherwise glue our line onto
    // whatever came last, silently breaking both settings.
    if (contents.length > 0 && !contents.endsWith('\n')) { contents += '\n'; }
    contents += SPI_USERCONFIG_COMMENT + '\n' + SPI_USERCONFIG_LINE + '\n';

    try {
        fs.writeFileSync(USERCONFIG_PATH, contents, 'utf8');
        self.logger.info('RetroTuner UI - added "' + SPI_USERCONFIG_LINE + '" to ' + USERCONFIG_PATH);
        return 'added';
    } catch (e) {
        self.logger.error('RetroTuner UI - could not write ' + USERCONFIG_PATH + ': ' + e);
        return String(e);
    }
};


module.exports = retrotunerui;
function retrotunerui(context) {
	var self = this;

	this.context = context;
	this.commandRouter = this.context.coreCommand;
	this.logger = this.context.logger;
	this.configManager = this.context.configManager;
}



retrotunerui.prototype.onVolumioStart = function()
{
	var self = this;
	var configFile=this.commandRouter.pluginManager.getConfigurationFile(this.context,'config.json');
	this.config = new (require('v-conf'))();
	this.config.loadFile(configFile);

    return libQ.resolve();
}

// Audio tap for the hidden level meter. cava reads this fifo and publishes the
// analysed bars; the python service only draws them. Gated on the asound config
// being present so the tap is never spliced into the output chain by accident.
var AUDIO_TAP_CONF = 'rt_in.rt_out.2.conf';
var AUDIO_TAP_FIFO = '/tmp/retrotuner-audio.fifo';

retrotunerui.prototype.setupAudioTap = function () {
    const self = this;
    const conf = __dirname + '/asound/' + AUDIO_TAP_CONF;

    if (!fs.existsSync(conf)) {
        // Logged rather than silent: otherwise "no audio tap" on the display is
        // indistinguishable from this code never having run at all.
        self.logger.info('RetroTuner UI - audio tap not enabled (no ' + conf + ')');
        return false;
    }

    // volumiofifo writes to the fifo but does not create it, so it has to exist
    // before the ALSA config referencing it is applied (stylish_player does the
    // same). Recreated each start so a stale non-fifo file can't wedge it.
    try {
        fs.removeSync(AUDIO_TAP_FIFO);
        // As the volumio user, not root: this process runs as root (see
        // serviceCmds) and the python service that reads the fifo does not.
        exec('/usr/bin/mkfifo -m 646 ' + AUDIO_TAP_FIFO, { uid: 1000, gid: 1000 }, function (e) {
            if (e) { self.logger.error('RetroTuner UI - could not create audio tap fifo: ' + e); }
        });
    } catch (e) {
        self.logger.error('RetroTuner UI - could not prepare audio tap fifo: ' + e);
    }

    // Volumio only folds plugin asound/ files into /etc/asound.conf when asked;
    // nothing does it implicitly on plugin start.
    try {
        self.commandRouter.executeOnPlugin('audio_interface', 'alsa_controller', 'updateALSAConfigFile');
        self.logger.info('RetroTuner UI - audio tap enabled (' + AUDIO_TAP_CONF + ')');
    } catch (e) {
        self.logger.error('RetroTuner UI - could not update the ALSA config: ' + e);
    }

    return true;
};

retrotunerui.prototype.onStart = function() {
    var self = this;

    const tapEnabled = self.setupAudioTap();

    // SPI is on by default, so a fresh install would otherwise never add the
    // boot parameter -- no settings save ever happens. Logged rather than
    // shown as a modal, since there may be no browser session at boot.
    if (Boolean(self.config.get('spi')) && self.ensureSpiInUserConfig() === 'added') {
        self.logger.warn('RetroTuner UI - SPI enabled in ' + USERCONFIG_PATH +
            '; a reboot is required before the MCP3008 can be read');
    }

    // Start pigpiod first (the python controls connect to it), then our service.
    return self.pigpiodServiceCmds('start')
        .then(function () {
            // Only when the tap is installed: without the tap cava has nothing
            // to read, and with the tap but no cava, playback stalls.
            return tapEnabled ? self.cavaServiceCmds('start') : libQ.resolve();
        })
        .then(function () { return self.retrotuneruiServiceCmds('start'); })
        .fail(function (e) { self.logger.error('RetroTuner UI - error starting: ' + e); });
};

retrotunerui.prototype.onStop = function() {
    var self = this;

    return self.retrotuneruiServiceCmds('stop')
        .then(function () { return self.cavaServiceCmds('stop'); })
        .then(function () { return self.pigpiodServiceCmds('stop'); })
        .fail(function (e) { self.logger.error('RetroTuner UI - error stopping: ' + e); });
};

retrotunerui.prototype.onRestart = function() {
    var self = this;

    // Mark this as our own restart so the controls don't show the shutdown
    // screen (only genuine stops/shutdowns should).
    try { fs.writeFileSync(RESTART_MARKER_PATH, String(Date.now())); }
    catch (e) { self.logger.error('RetroTuner UI - could not write restart marker: ' + e); }

    // Only restart our own service. Use 'start' (not 'restart') for pigpiod so a
    // running daemon is left untouched — restarting it here races the controls'
    // pigpio reconnect and leaves the rotary encoder dead until the next restart.
    // Config changes never require pigpiod to restart.
    //
    // cava is deliberately left alone too, and for a sharper reason: it is the
    // only reader of the audio tap, and bouncing it would leave the fifo unread
    // long enough for ALSA to block and playback to stop. Settings saves restart
    // this plugin routinely, so that would cut the music every time.
    return self.pigpiodServiceCmds('start')
        .then(function () { return self.retrotuneruiServiceCmds('restart'); })
        .fail(function (e) { self.logger.error('RetroTuner UI - error restarting: ' + e); });
};

// Called from the "Restart Now" button of the SPI-mode-changed modal (see
// saveOptions) via Volumio's callMethod mechanism -- a plugin restart can't
// apply an SPI/bitbang switch, only a full device reboot re-probes the pin mux.
retrotunerui.prototype.rebootNow = function () {
    this.commandRouter.reboot();
    return libQ.resolve();
};


// Configuration Methods -----------------------------------------------------------------------------

retrotunerui.prototype.getUIConfig = function() {
    const self = this;
    const defer = libQ.defer();

    this.logger.info('RetroTuner UI - getUIConfig');

    const lang_code = this.commandRouter.sharedVars.get('language_code');

    this.commandRouter.i18nJson(__dirname + '/i18n/strings_' + lang_code + '.json',
        __dirname + '/i18n/strings_en.json',
        __dirname + '/UIConfig.json')
        .then(function (uiconf) {
            // Look sections and content up by id, not numeric index, so adding or
            // reordering sections can never silently shift indices (which has
            // broken this page before).
            function section(id) {
                return uiconf.sections.find(function (s) { return s.id === id; });
            }
            function setValue(sec, contentId, value) {
                if (!sec) { return; }
                const item = sec.content.find(function (c) { return c.id === contentId; });
                if (item) { item.value = value; }
            }

            const pins = section('buttons');
            setValue(pins, 'spi', self.config.get('spi'));
            setValue(pins, 'spi_bus', self.config.get('spi_bus'));
            setValue(pins, 'buttons_clk', self.config.get('buttons_clk'));
            setValue(pins, 'buttons_miso', self.config.get('buttons_miso'));
            setValue(pins, 'buttons_mosi', self.config.get('buttons_mosi'));
            setValue(pins, 'buttons_cs', self.config.get('buttons_cs'));
            setValue(pins, 'buttons_channel1', self.config.get('buttons_channel1'));
            setValue(pins, 'buttons_channel2', self.config.get('buttons_channel2'));

            // Capture section: action buttons have no stored values, but we
            // rewrite each "Configure" label to show its current mapping so the
            // user can see what's set without opening the Advanced section.
            const capture = section('button_capture');
            if (capture) {
                capture.content.forEach(function (item) {
                    if (item.id && item.id.indexOf('capture_btn_') === 0) {
                        const key = item.id.slice('capture_'.length);  // capture_btn_x -> btn_x
                        const friendly = CAPTURE_LABELS[key] || key;
                        const val = self.config.get(key);
                        item.label = 'Configure ' + friendly + (val ? ' (now: ' + val + ')' : ' (unmapped)');
                    }
                });
            }

            // Also new -- default matches apply_log_level()'s own fallback in index.py.
            setValue(section('diagnostics'), 'debug_mode', self.config.get('debug_mode', false));

            const encoder = section('encoder');
            setValue(encoder, 'rot_enc_A', self.config.get('rot_enc_A'));
            setValue(encoder, 'rot_enc_B', self.config.get('rot_enc_B'));
            // New setting -- default matches menu_manager's own fallback for an
            // older config that predates it (rotary skip stays off).
            setValue(encoder, 'rotary_skip_track', self.config.get('rotary_skip_track', false));

            const lcd = section('lcd');
            setValue(lcd, 'lcd_rs', self.config.get('lcd_rs'));
            setValue(lcd, 'lcd_e', self.config.get('lcd_e'));
            setValue(lcd, 'lcd_d4', self.config.get('lcd_d4'));
            setValue(lcd, 'lcd_d5', self.config.get('lcd_d5'));
            setValue(lcd, 'lcd_d6', self.config.get('lcd_d6'));
            setValue(lcd, 'lcd_d7', self.config.get('lcd_d7'));

            const advanced = section('button_advanced');
            setValue(advanced, 'button_poll_rate', self.config.get('button_poll_rate'));
            setValue(advanced, 'button_debounce_rate', self.config.get('button_debounce_rate'));
            setValue(advanced, 'button_cooldown_rate', self.config.get('button_cooldown_rate'));
            // Added after these shipped, so an old config may not have them -- v-conf's
            // get() then returns undefined, leaving the field blank, and an unedited
            // Save submits an empty string that isValid() silently rejects. Must match
            // controls.py's ADC_SAMPLES/BUTTON_HYSTERESIS -- keep the two in sync.
            setValue(advanced, 'button_samples', self.config.get('button_samples', 5));
            setValue(advanced, 'button_hysteresis', self.config.get('button_hysteresis', 6));
            defer.resolve(uiconf);
        })
        .fail(function (error) {
            self.logger.error('RetroTuner UI - Failed to parse UI Configuration page:' + error);
            defer.reject(new Error());
        });

    return defer.promise;
};

retrotunerui.prototype.saveOptions = function (data) {
    const self = this;

    // Captured before the save loop below overwrites it. A plugin restart
    // can't apply this -- the pin mux is only reapplied when the SPI driver
    // probes at boot -- so this is flagged separately from the usual restart.
    const spiModeChanged = data.hasOwnProperty('spi') &&
        Boolean(data.spi) !== Boolean(self.config.get('spi'));

    // Every field saved through this form is either a switch (spi, debug_mode)
    // or a plain numeric setting (a GPIO pin or a rate) -- button mappings are
    // only ever written by the capture flow (self.config.set() below), which
    // bypasses this validation entirely, so no button-mapping grammar needs
    // handling here.
    function isValid(value) {
        if (typeof value === 'boolean') {
            return true;
        }
        return !isNaN(parseFloat(value)) && isFinite(value);
    }

    self.logger.info('RetroTuner UI - saving settings');

    const formattedJsonString = JSON.stringify(data, null, 2);
    // console.log(formattedJsonString);

    // Parse JSON string into a JavaScript object
    const jsonObject = JSON.parse(formattedJsonString);

    // Iterate through the object and save if the item is valid
    for (const key in jsonObject) {
        if (jsonObject.hasOwnProperty(key)) {
            const value = jsonObject[key];
            // console.log(`${key}: ${value}`);
            if (isValid(value)) {
                self.config.set(key, value);
            } else {
                self.logger.error(`${value} is not a valid number or boolean. Not saving ${key}.`);
                this.commandRouter.pushToastMessage('fail', ("RetroTuner UI"), (`${value} is not a valid number or boolean. Not saving ${key}.`));
            }
        }
    }
    
    self.logger.info('RetroTuner UI - settings saved');
    this.commandRouter.pushToastMessage('success', ("RetroTuner UI"), this.commandRouter.getI18nString("COMMON.CONFIGURATION_UPDATE_DESCRIPTION"));

    // SPI is the default mode, so the boot config is checked whenever SPI is on
    // -- not only when the switch changes -- otherwise a fresh install never
    // gets the line added at all.
    const bootConfig = Boolean(self.config.get('spi')) ? self.ensureSpiInUserConfig() : 'present';
    const rebootPending = spiModeChanged || bootConfig === 'added';

    if (bootConfig !== 'present' && bootConfig !== 'added') {
        // Writing failed (read-only /boot, permissions). Rebooting would not
        // help, so don't offer a "Restart Now" that leads nowhere.
        self.commandRouter.broadcastMessage('openModal', {
            title: 'SPI Mode Enabled -- Boot Config Not Writable',
            message: 'SPI mode is enabled, but "' + SPI_USERCONFIG_LINE + '" could not be added to ' +
                USERCONFIG_PATH + ' (' + bootConfig + '). Add that line manually via SSH and reboot, ' +
                'otherwise button reads will come back as a constant 0.',
            size: 'lg',
            buttons: [{ name: 'OK', class: 'btn btn-default' }]
        });
    } else if (rebootPending) {
        // Same shape as Volumio's own "I2S DAC enabled" prompt: a modal with a
        // one-click restart, since a plugin restart can't apply either change.
        self.commandRouter.broadcastMessage('openModal', {
            title: bootConfig === 'added' ? 'SPI Enabled -- Reboot Required' : 'SPI Mode Changed',
            message: bootConfig === 'added'
                ? '"' + SPI_USERCONFIG_LINE + '" has been added to ' + USERCONFIG_PATH +
                  '. Reboot to load the SPI driver -- until then button reads return 0.'
                : 'SPI mode has been changed, restart the system for changes to take effect.',
            size: 'lg',
            buttons: [
                {
                    name: 'Restart Now',
                    class: 'btn btn-info',
                    emit: 'callMethod',
                    payload: { endpoint: 'user_interface/retrotuner-ui', method: 'rebootNow', data: '' }
                },
                {
                    name: 'Later',
                    class: 'btn btn-default'
                }
            ]
        });
    }

    const noConflicts = self._checkButtonConflicts();  // always run: pushes its own toast on conflict

    if (rebootPending) {
        // A plugin restart can't apply an SPI/bitbang switch or a new boot
        // parameter -- only a reboot re-probes the pin mux -- and restarting
        // right now would try to open a kernel SPI device that doesn't exist
        // yet, crashing the controls thread. The modal above is the only path
        // forward here; this restart happens implicitly once they reboot.
        self.logger.info('RetroTuner UI - reboot pending; not restarting the plugin');
    } else if (noConflicts) {
        self.logger.info('RetroTuner UI - restarting services');
        self.onRestart();
    }

    return libQ.resolve();
};


retrotunerui.prototype.getConfigurationFiles = function() {
	return ['config.json'];
}

// Button capture ("learn") -------------------------------------------------

var CAPTURE_FLAG_PATH = '/tmp/retrotuner-ui-capture-on';
var CAPTURE_READING_PATH = '/tmp/retrotuner-ui-capture.json';
var CAPTURE_BASELINE_PATH = '/tmp/retrotuner-ui-capture-baseline.json';
var CAPTURE_IDLE_TIMEOUT_MS = 90000;  // auto-resume controls after this much inactivity
var CAPTURE_POLL_MS = 200;
var BASELINE_SETTLE_MS = 5000;  // how long the user must leave the buttons alone

// Capture works on raw ADC readings, which never land on exactly the same
// count twice, so confirming a capture is a tolerance test and what gets
// stored is a band around the midpoint of the two readings.
var ADC_MAX = 1023;
// Sized against a measured Teac ladder (adjacent buttons 69-141 counts apart --
// keep in sync with controls.py's BUTTON_HYSTERESIS). The dominant error is
// contact resistance, not noise -- an aged switch read 21 counts higher on a
// light press than a firm one, so both the band and the confirm tolerance
// have to cover that much press-to-press variation.
var CAPTURE_CONFIRM_TOLERANCE = 15;  // counts two presses may differ by and still confirm
var CAPTURE_BAND_HALF_WIDTH = 25;    // half-width of the band written to the config

// Raw 0 is what an unreachable MCP3008 returns on every channel, so no band may
// claim it -- otherwise a dead SPI bus reads as one button held down forever.
var MIN_BAND_VALUE = 1;

// The band a captured reading is stored as. One definition: a press and a
// resting value are banded the same way, and the clamps only exist once.
function bandAround(centre) {
    return Math.max(centre - CAPTURE_BAND_HALF_WIDTH, MIN_BAND_VALUE) + '-' +
           Math.min(centre + CAPTURE_BAND_HALF_WIDTH, ADC_MAX);
}

// config key -> friendly label shown in toasts
var CAPTURE_LABELS = {
    btn_enter: 'Enter',
    btn_radio: 'Radio',
    btn_spotify: 'Spotify',
    btn_info: 'Info',
    btn_favourite: 'Favourite',
    btn_pause: 'Pause/Play',
    btn_sleep_timer: 'Sleep Timer',
    btn_dimmer: 'Dimmer',
    btn_main_menu: 'Main Menu',
    btn_back: 'Back'
};

// Pushes the current getUIConfig() output to any open settings page, so a
// value changed here (capture, clear, base resistance) shows up immediately
// instead of needing a manual page reload.
retrotunerui.prototype._refreshUiConfig = function () {
    const self = this;
    self.commandRouter.getUIConfigOnPlugin('user_interface', 'retrotuner-ui', {})
        .then(function (config) {
            self.commandRouter.broadcastMessage('pushUiConfig', config);
        })
        .fail(function (e) {
            self.logger.error('RetroTuner UI - could not refresh settings page: ' + e);
        });
};

// Conflict detection helpers ---------------------------------------------------

function parseButtonMapping(str) {
    if (!str) return null;
    const commaIdx = str.indexOf(',');
    if (commaIdx === -1) return null;
    const channel = parseInt(str.slice(0, commaIdx).trim(), 10);
    const valuePart = str.slice(commaIdx + 1).trim();
    if (isNaN(channel) || !valuePart) return null;
    if (valuePart.includes('-')) {
        const parts = valuePart.split('-').map(function (s) { return parseInt(s.trim(), 10); });
        if (parts.some(isNaN)) return null;
        return { channel: channel, type: 'range', low: Math.min.apply(null, parts), high: Math.max.apply(null, parts) };
    }
    const value = parseInt(valuePart, 10);
    if (isNaN(value)) return null;
    return { channel: channel, type: 'value', value: value };
}

function mappingsOverlap(a, b) {
    if (a.channel !== b.channel) return false;
    if (a.type === 'value' && b.type === 'value') return a.value === b.value;
    if (a.type === 'range' && b.type === 'range') return a.low <= b.high && b.low <= a.high;
    const point = a.type === 'value' ? a.value : b.value;
    const range  = a.type === 'range'  ? a       : b;
    return point >= range.low && point <= range.high;
}

// One entry point per button (UIConfig button onClick targets these by name)
retrotunerui.prototype.captureBtnEnter = function () { return this.startCapture('btn_enter'); };
retrotunerui.prototype.captureBtnRadio = function () { return this.startCapture('btn_radio'); };
retrotunerui.prototype.captureBtnSpotify = function () { return this.startCapture('btn_spotify'); };
retrotunerui.prototype.captureBtnInfo = function () { return this.startCapture('btn_info'); };
retrotunerui.prototype.captureBtnFavourite = function () { return this.startCapture('btn_favourite'); };
retrotunerui.prototype.captureBtnPause = function () { return this.startCapture('btn_pause'); };
retrotunerui.prototype.captureBtnSleepTimer = function () { return this.startCapture('btn_sleep_timer'); };
retrotunerui.prototype.captureBtnDimmer = function () { return this.startCapture('btn_dimmer'); };
retrotunerui.prototype.captureBtnMainMenu = function () { return this.startCapture('btn_main_menu'); };
retrotunerui.prototype.captureBtnBack = function () { return this.startCapture('btn_back'); };

// Clear the button currently selected for capture. Folded into the same
// staged session as capturing, so one "Save & Restart Controls" applies both --
// choose the button via "Configure", then click "Clear Selected Button".
retrotunerui.prototype.clearSelectedButton = function () {
    const self = this;
    const session = self._captureSession;
    if (!session || !session.target) {
        self.commandRouter.pushToastMessage('info', 'Button Capture',
            'First click a "Configure" button to choose which button to clear, then click "Clear Selected Button".');
        return libQ.resolve();
    }

    const key = session.target;
    const label = session.label;
    self.config.set(key, '');
    if (!self._capturedValues) { self._capturedValues = {}; }
    self._capturedValues[label] = 'cleared';
    self.logger.info('RetroTuner UI - cleared mapping for ' + label);

    // Deselect so a stray press can't re-capture the button we just cleared.
    session.target = null;
    session.candidate = null;
    session.deadline = Date.now() + CAPTURE_IDLE_TIMEOUT_MS;

    self.commandRouter.pushToastMessage('success', 'Button Capture',
        '"' + label + '" cleared. Configure another button, or click "Save & Restart Controls".');
    self._refreshUiConfig();
    return libQ.resolve();
};

// Begin a capture session if one isn't already running. Keeps the controls
// paused (via the flag file) until the user saves or goes idle. Returns
// false if the session could not be started.
retrotunerui.prototype.ensureCaptureSession = function () {
    const self = this;
    if (self._captureSession) { return true; }

    try {
        fs.writeFileSync(CAPTURE_FLAG_PATH, '');
    } catch (e) {
        self.logger.error('RetroTuner UI - could not start capture: ' + e);
        self.commandRouter.pushToastMessage('error', 'Button Capture', 'Could not start capture mode.');
        return false;
    }
    try { fs.removeSync(CAPTURE_READING_PATH); } catch (e) {}
    try { fs.removeSync(CAPTURE_BASELINE_PATH); } catch (e) {}
    self._captureSession = { lastSeq: null };
    self._captureTimer = setInterval(function () { self.pollCapture(); }, CAPTURE_POLL_MS);
    return true;
};

retrotunerui.prototype.startCapture = function (targetKey) {
    const self = this;
    const label = CAPTURE_LABELS[targetKey] || targetKey;

    if (!self.ensureCaptureSession()) { return libQ.resolve(); }

    // (Re)target the session at the button the user just clicked.
    self._captureSession.target = targetKey;
    self._captureSession.label = label;
    self._captureSession.candidate = null;
    self._captureSession.deadline = Date.now() + CAPTURE_IDLE_TIMEOUT_MS;

    self.commandRouter.pushToastMessage('info', 'Button Capture',
        'Controls paused. Press the "' + label + '" button on the unit, or click "Clear Selected Button" to unmap it.');
    return libQ.resolve();
};

retrotunerui.prototype.pollCapture = function () {
    const self = this;
    const session = self._captureSession;
    if (!session) { self.endCaptureSession(); return; }

    if (Date.now() > session.deadline) {
        self.endCaptureSession();
        self.commandRouter.pushToastMessage('info', 'Button Capture',
            'Capture mode ended after inactivity. Controls resumed.');
        return;
    }

    let reading;
    try {
        if (!fs.existsSync(CAPTURE_READING_PATH)) { return; }
        reading = fs.readJsonSync(CAPTURE_READING_PATH);
    } catch (e) {
        return;  // partial write; try again next tick
    }

    if (reading == null || reading.seq == null) { return; }
    if (reading.seq === session.lastSeq) { return; }   // no new press since last poll
    session.lastSeq = reading.seq;
    session.deadline = Date.now() + CAPTURE_IDLE_TIMEOUT_MS;  // any press keeps the session alive

    if (!session.target) { return; }   // a press arrived but no button is selected yet

    // Each new seq is one detected physical press (Python already filters out
    // the resting value and key-release).
    const ch = reading.channel;
    const val = reading.value;

    if (session.candidate == null) {
        session.candidate = { channel: ch, value: val };
        self.commandRouter.pushToastMessage('info', 'Button Capture',
            'Read channel ' + ch + ', value ' + val + '. Press "' + session.label + '" again to confirm.');
        return;
    }

    if (session.candidate.channel === ch && Math.abs(session.candidate.value - val) <= CAPTURE_CONFIRM_TOLERANCE) {
        // Store a band around the midpoint of the two presses. A raw reading is
        // never identical twice, so a point value could never match again.
        const centre = Math.round((session.candidate.value + val) / 2);
        const configValue = ch + ', ' + bandAround(centre);
        if (!self._capturedValues) { self._capturedValues = {}; }

        // Auto-reassign: if this value already belongs to other actions, clear
        // them so the physical button now triggers only the action just learnt.
        const displaced = self._findConflictingButtons(session.target, ch, centre);
        displaced.forEach(function (other) {
            self.config.set(other.key, '');
            self._capturedValues[other.label] = 'cleared';
        });

        self.config.set(session.target, configValue);
        self._capturedValues[session.label] = configValue;

        let msg = 'Captured "' + session.label + '" = ' + configValue + '.';
        if (displaced.length > 0) {
            msg += ' Reassigned from ' + displaced.map(function (d) { return '"' + d.label + '"'; }).join(', ') + '.';
        }
        msg += ' Configure another button, or click "Save & Restart Controls".';
        self.commandRouter.pushToastMessage(displaced.length > 0 ? 'warning' : 'success', 'Button Capture', msg);
        self._refreshUiConfig();

        // Stay in the session (controls remain paused); wait for the next button.
        session.target = null;
        session.candidate = null;
    } else {
        session.candidate = { channel: ch, value: val };
        self.commandRouter.pushToastMessage('info', 'Button Capture',
            'Got a different value (channel ' + ch + ', value ' + val + '). Press "' + session.label + '" again to confirm.');
    }
};

// Capture the resting (no-press) value of both ADC channels. The python side
// measures each channel's baseline as soon as capture mode starts; we just
// need the user to leave the buttons alone for a moment, then read it back.
retrotunerui.prototype.captureBaseResistance = function () {
    const self = this;

    if (!self.ensureCaptureSession()) { return libQ.resolve(); }

    const session = self._captureSession;
    // Deselect any pending button target; this capture wants no presses at all.
    session.target = null;
    session.candidate = null;
    session.deadline = Date.now() + CAPTURE_IDLE_TIMEOUT_MS;

    const startSeq = session.lastSeq;
    self.commandRouter.pushToastMessage('info', 'Button Capture',
        'Capturing base resistance. Do NOT press any buttons for the next ' +
        (BASELINE_SETTLE_MS / 1000) + ' seconds...');

    setTimeout(function () { self.finishBaseResistanceCapture(session, startSeq); }, BASELINE_SETTLE_MS);
    return libQ.resolve();
};

retrotunerui.prototype.finishBaseResistanceCapture = function (session, startSeq) {
    const self = this;
    if (self._captureSession !== session) { return; }  // session ended in the meantime

    if (session.lastSeq !== startSeq) {
        self.commandRouter.pushToastMessage('error', 'Button Capture',
            'A button press was detected while capturing the base resistance. Try again without pressing anything.');
        return;
    }

    let baselines = null;
    try { baselines = fs.readJsonSync(CAPTURE_BASELINE_PATH); } catch (e) {}

    const ch1 = self.config.get('buttons_channel1');
    const ch2 = self.config.get('buttons_channel2');
    const val1 = baselines ? baselines[String(ch1)] : null;
    const val2 = baselines ? baselines[String(ch2)] : null;

    if (val1 == null || val2 == null) {
        self.commandRouter.pushToastMessage('error', 'Button Capture',
            'Could not read the base resistance for both channels. Wait a moment and try again.');
        return;
    }

    if (!self._capturedValues) { self._capturedValues = {}; }
    const rest1 = ch1 + ', ' + bandAround(val1);
    const rest2 = ch2 + ', ' + bandAround(val2);
    self.config.set('btn_no_press_channel1', rest1);
    self.config.set('btn_no_press_channel2', rest2);
    self._capturedValues['No Press Channel 1'] = rest1;
    self._capturedValues['No Press Channel 2'] = rest2;

    self.commandRouter.pushToastMessage('success', 'Button Capture',
        'Captured base resistance: channel ' + ch1 + ' = ' + val1 + ', channel ' + ch2 + ' = ' + val2 +
        '. Configure another button, or click "Save & Restart Controls".');
    self._refreshUiConfig();
};

retrotunerui.prototype.endCaptureSession = function () {
    const self = this;
    if (self._captureTimer) {
        clearInterval(self._captureTimer);
        self._captureTimer = null;
    }
    self._captureSession = null;
    try { fs.removeSync(CAPTURE_FLAG_PATH); } catch (e) {}
    try { fs.removeSync(CAPTURE_READING_PATH); } catch (e) {}
    try { fs.removeSync(CAPTURE_BASELINE_PATH); } catch (e) {}
};

// Apply everything captured this session and restart the controls once.
retrotunerui.prototype.saveCapture = function () {
    const self = this;

    self.endCaptureSession();   // resume controls

    const captured = self._capturedValues || {};
    const labels = Object.keys(captured);

    if (labels.length === 0) {
        self.commandRouter.pushToastMessage('info', 'Button Capture',
            'No new button captures to save.');
        return libQ.resolve();
    }

    const summary = labels.map(function (label) { return label + ' = ' + captured[label]; }).join(', ');
    self._capturedValues = {};

    if (!self._checkButtonConflicts()) {
        self.commandRouter.pushToastMessage('info', 'Button Capture',
            'Values saved (' + summary + ') but restart blocked — fix the conflict above first.');
        self._refreshUiConfig();
        return libQ.resolve();
    }

    self.commandRouter.pushToastMessage('success', 'Button Capture',
        'Saved (' + summary + '). Restarting controls...');
    self._refreshUiConfig();
    self.onRestart();
    return libQ.resolve();
};

// Returns [{key, label}] of OTHER buttons whose mapping overlaps the band a
// reading at `centre` would be stored as — used to auto-reassign on capture.
retrotunerui.prototype._findConflictingButtons = function (targetKey, channel, centre) {
    const self = this;
    const band = bandAround(centre).split('-');
    const candidate = { channel: channel, type: 'range',
                        low: parseInt(band[0], 10), high: parseInt(band[1], 10) };
    return Object.keys(CAPTURE_LABELS)
        .filter(function (key) { return key !== targetKey; })
        .map(function (key) {
            return { key: key, label: CAPTURE_LABELS[key], parsed: parseButtonMapping(self.config.get(key)) };
        })
        .filter(function (m) { return m.parsed !== null && mappingsOverlap(candidate, m.parsed); })
        .map(function (m) { return { key: m.key, label: m.label }; });
};

// Returns true if no conflicts exist; false and fires an error toast if any are found.
retrotunerui.prototype._checkButtonConflicts = function () {
    const self = this;
    const mappings = Object.keys(CAPTURE_LABELS)
        .map(function (key) {
            return { label: CAPTURE_LABELS[key], parsed: parseButtonMapping(self.config.get(key)) };
        })
        .filter(function (m) { return m.parsed !== null; });

    const conflicts = [];
    for (let i = 0; i < mappings.length; i++) {
        for (let j = i + 1; j < mappings.length; j++) {
            if (mappingsOverlap(mappings[i].parsed, mappings[j].parsed)) {
                conflicts.push('"' + mappings[i].label + '" and "' + mappings[j].label + '"');
            }
        }
    }

    if (conflicts.length > 0) {
        self.logger.error('RetroTuner UI - button conflicts detected: ' + conflicts.join('; '));
        self.commandRouter.pushToastMessage('error', 'RetroTuner UI',
            'Button conflict: ' + conflicts.join('; ') + '. Restart blocked — please remap before saving.');
        return false;
    }
    return true;
};

// Plugin methods -----------------------------------------------------------------------------

// Run a systemctl command asynchronously so Volumio's event loop is never
// blocked while systemd works (a restart can wait on the old process to stop).
retrotunerui.prototype.systemctl = function (cmd, unit) {
    var self = this;

    if (!['start', 'stop', 'restart'].includes(cmd)) {
        return libQ.reject(new TypeError('Unknown systemd command: ' + cmd));
    }

    const defer = libQ.defer();
    exec(`/usr/bin/sudo /bin/systemctl ${cmd} ${unit} -q`, { uid: 1000, gid: 1000 }, function (error, stdout, stderr) {
        if (error) {
            self.logger.error(`RetroTuner UI - unable to ${cmd} ${unit}: ${error}`);
            defer.reject(error);
            return;
        }
        if (stderr) {
            self.logger.error(`RetroTuner UI - ${cmd} ${unit} stderr: ${stderr}`);
        }
        self.logger.info(`RetroTuner UI - ${unit} ${cmd} complete`);
        defer.resolve();
    });

    return defer.promise;
};

retrotunerui.prototype.retrotuneruiServiceCmds = function (cmd) {
    return this.systemctl(cmd, 'retrotuner-ui.service');
};

retrotunerui.prototype.pigpiodServiceCmds = function (cmd) {
    return this.systemctl(cmd, 'pigpiod.service');
};

// cava reads the audio tap and publishes the analysed bars. It must run for as
// long as the tap is in the ALSA chain -- not just while the meter is on screen
// -- because an unread fifo blocks ALSA and stops playback.
retrotunerui.prototype.cavaServiceCmds = function (cmd) {
    return this.systemctl(cmd, 'retrotuner-cava.service');
};
