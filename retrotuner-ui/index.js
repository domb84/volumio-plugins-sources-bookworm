'use strict';

var libQ = require('kew');
var fs = require('fs-extra');
var exec = require('child_process').exec;

// Dropped just before a self-triggered restart so the python service can tell a
// restart (capture/settings save) apart from a genuine stop/shutdown.
var RESTART_MARKER_PATH = '/tmp/retrotuner-ui-restarting';


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

retrotunerui.prototype.onStart = function() {
    var self = this;

    // Start pigpiod first (the python controls connect to it), then our service.
    return self.pigpiodServiceCmds('start')
        .then(function () { return self.retrotuneruiServiceCmds('start'); })
        .fail(function (e) { self.logger.error('RetroTuner UI - error starting: ' + e); });
};

retrotunerui.prototype.onStop = function() {
    var self = this;

    return self.retrotuneruiServiceCmds('stop')
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
    return self.pigpiodServiceCmds('start')
        .then(function () { return self.retrotuneruiServiceCmds('restart'); })
        .fail(function (e) { self.logger.error('RetroTuner UI - error restarting: ' + e); });
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

    if (self._checkButtonConflicts()) {
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
        return libQ.resolve();
    }

    self.commandRouter.pushToastMessage('success', 'Button Capture',
        'Saved (' + summary + '). Restarting controls...');
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
