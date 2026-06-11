'use strict';

var libQ = require('kew');
var fs = require('fs-extra');
var exec = require('child_process').exec;

// Dropped just before a self-triggered restart so the python service can tell a
// restart (capture/settings save) apart from a genuine stop/shutdown.
var RESTART_MARKER_PATH = '/tmp/teac-dab-controls-restarting';


module.exports = teacdabcontrols;
function teacdabcontrols(context) {
	var self = this;

	this.context = context;
	this.commandRouter = this.context.coreCommand;
	this.logger = this.context.logger;
	this.configManager = this.context.configManager;
}



teacdabcontrols.prototype.onVolumioStart = function()
{
	var self = this;
	var configFile=this.commandRouter.pluginManager.getConfigurationFile(this.context,'config.json');
	this.config = new (require('v-conf'))();
	this.config.loadFile(configFile);

    return libQ.resolve();
}

teacdabcontrols.prototype.onStart = function() {
    var self = this;

    // Start pigpiod first (the python controls connect to it), then our service.
    return self.pigpiodServiceCmds('start')
        .then(function () { return self.teacdabcontrolsServiceCmds('start'); })
        .fail(function (e) { self.logger.error('Teac DAB Controls - error starting: ' + e); });
};

teacdabcontrols.prototype.onStop = function() {
    var self = this;

    return self.teacdabcontrolsServiceCmds('stop')
        .then(function () { return self.pigpiodServiceCmds('stop'); })
        .fail(function (e) { self.logger.error('Teac DAB Controls - error stopping: ' + e); });
};

teacdabcontrols.prototype.onRestart = function() {
    var self = this;

    // Mark this as our own restart so the controls don't show the shutdown
    // screen (only genuine stops/shutdowns should).
    try { fs.writeFileSync(RESTART_MARKER_PATH, String(Date.now())); }
    catch (e) { self.logger.error('Teac DAB Controls - could not write restart marker: ' + e); }

    // Only restart our own service. Use 'start' (not 'restart') for pigpiod so a
    // running daemon is left untouched — restarting it here races the controls'
    // pigpio reconnect and leaves the rotary encoder dead until the next restart.
    // Config changes never require pigpiod to restart.
    return self.pigpiodServiceCmds('start')
        .then(function () { return self.teacdabcontrolsServiceCmds('restart'); })
        .fail(function (e) { self.logger.error('Teac DAB Controls - error restarting: ' + e); });
};


// Configuration Methods -----------------------------------------------------------------------------

teacdabcontrols.prototype.getUIConfig = function() {
    const self = this;
    const defer = libQ.defer();

    this.logger.info('Teac DAB Controls - getUIConfig');

    const lang_code = this.commandRouter.sharedVars.get('language_code');

    this.commandRouter.i18nJson(__dirname + '/i18n/strings_' + lang_code + '.json',
        __dirname + '/i18n/strings_en.json',
        __dirname + '/UIConfig.json')
        .then(function (uiconf) {
            uiconf.sections[0].content[0].value = self.config.get('spi');
            uiconf.sections[0].content[1].value = self.config.get('spi_bus');
            uiconf.sections[0].content[2].value = self.config.get('buttons_clk');
            uiconf.sections[0].content[3].value = self.config.get('buttons_miso');
            uiconf.sections[0].content[4].value = self.config.get('buttons_mosi');
            uiconf.sections[0].content[5].value = self.config.get('buttons_cs');
            uiconf.sections[0].content[6].value = self.config.get('buttons_channel1');
            uiconf.sections[0].content[7].value = self.config.get('buttons_channel2');
            uiconf.sections[0].content[8].value = self.config.get('button_poll_rate');
            uiconf.sections[0].content[9].value = self.config.get('button_debounce_rate');
            uiconf.sections[0].content[10].value = self.config.get('button_cooldown_rate');
            // sections[1] is "Configure Buttons (Capture)" — action buttons, no stored values
            uiconf.sections[2].content[0].value = self.config.get('rot_enc_A');
            uiconf.sections[2].content[1].value = self.config.get('rot_enc_B');
            uiconf.sections[3].content[0].value = self.config.get('lcd_rs');
            uiconf.sections[3].content[1].value = self.config.get('lcd_e');
            uiconf.sections[3].content[2].value = self.config.get('lcd_d4');
            uiconf.sections[3].content[3].value = self.config.get('lcd_d5');
            uiconf.sections[3].content[4].value = self.config.get('lcd_d6');
            uiconf.sections[3].content[5].value = self.config.get('lcd_d7');
            // sections[4] is the advanced section; content[0] is the "Edit values manually" toggle
            uiconf.sections[4].content[1].value = self.config.get('btn_enter');
            uiconf.sections[4].content[2].value = self.config.get('btn_radio');
            uiconf.sections[4].content[3].value = self.config.get('btn_spotify');
            uiconf.sections[4].content[4].value = self.config.get('btn_info');
            uiconf.sections[4].content[5].value = self.config.get('btn_favourite');
            uiconf.sections[4].content[6].value = self.config.get('btn_main_menu');
            uiconf.sections[4].content[7].value = self.config.get('btn_back');
            uiconf.sections[4].content[8].value = self.config.get('btn_no_press_channel1');
            uiconf.sections[4].content[9].value = self.config.get('btn_no_press_channel2');
            uiconf.sections[4].content[10].value = self.config.get('btn_pause');
            uiconf.sections[4].content[11].value = self.config.get('btn_remove_favourite');
            uiconf.sections[4].content[12].value = self.config.get('btn_sleep_timer');
            uiconf.sections[4].content[13].value = self.config.get('btn_cancel_sleep_timer');
            uiconf.sections[4].content[14].value = self.config.get('btn_dimmer');
            defer.resolve(uiconf);
        })
        .fail(function () {
            self.logger.error('Teac DAB Controls - Failed to parse UI Configuration page:' + error);
            defer.reject(new Error());
        });

    return defer.promise;
};

teacdabcontrols.prototype.saveOptions = function (data) {
    const self = this;

    // Function to check if a value is numeric, boolean, or comma-separated numbers
    function isValid(value) {
        // Check if the value is a boolean
        if (typeof value === 'boolean') {
            return true;
        }
        
        // Check if the value is a comma-separated list of numbers
        if (typeof value === 'string' && value.match(/^\s*(\d+\s*,\s*)*\d+\s*$/)) {
            return true;
        }
        
        // Check if the value is a single numeric value
        return !isNaN(parseFloat(value)) && isFinite(value);
    }

    self.logger.info('Teac DAB Controls - saving settings');

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
                // console.log(`${value} is a valid number, comma seperated numbers or boolean. Saving ${key}.`);
                self.config.set(key, value);
            } else {
                self.logger.error(`${value} is not a valid number, comma seperated numbers or boolean. Not saving ${key}.`);
                this.commandRouter.pushToastMessage('fail', ("Teac DAB Controls"), (`${value} is not a valid number, comma seperated numbers or boolean. Not saving ${key}.`));
            }
        }
    }
    
    this.commandRouter.pushToastMessage('success', ("Teac DAB Controls"), this.commandRouter.getI18nString("COMMON.CONFIGURATION_UPDATE_DESCRIPTION"));

    self.logger.info('Teac DAB Controls - settings saved');
    self.logger.info('Teac DAB Controls - restarting services');
    self.onRestart()

    return libQ.resolve();
};


teacdabcontrols.prototype.getConfigurationFiles = function() {
	return ['config.json'];
}

// Button capture ("learn") -------------------------------------------------

var CAPTURE_FLAG_PATH = '/tmp/teac-dab-controls-capture-on';
var CAPTURE_READING_PATH = '/tmp/teac-dab-controls-capture.json';
var CAPTURE_BASELINE_PATH = '/tmp/teac-dab-controls-capture-baseline.json';
var CAPTURE_IDLE_TIMEOUT_MS = 90000;  // auto-resume controls after this much inactivity
var CAPTURE_POLL_MS = 200;
var BASELINE_SETTLE_MS = 5000;  // how long the user must leave the buttons alone

// config key -> friendly label shown in toasts
var CAPTURE_LABELS = {
    btn_enter: 'Enter',
    btn_radio: 'Radio',
    btn_spotify: 'Spotify',
    btn_info: 'Info',
    btn_favourite: 'Favourite',
    btn_pause: 'Pause/Play',
    btn_remove_favourite: 'Remove Favourite',
    btn_sleep_timer: 'Sleep Timer',
    btn_cancel_sleep_timer: 'Cancel Sleep Timer',
    btn_dimmer: 'Dimmer',
    btn_main_menu: 'Main Menu',
    btn_back: 'Back'
};

// One entry point per button (UIConfig button onClick targets these by name)
teacdabcontrols.prototype.captureBtnEnter = function () { return this.startCapture('btn_enter'); };
teacdabcontrols.prototype.captureBtnRadio = function () { return this.startCapture('btn_radio'); };
teacdabcontrols.prototype.captureBtnSpotify = function () { return this.startCapture('btn_spotify'); };
teacdabcontrols.prototype.captureBtnInfo = function () { return this.startCapture('btn_info'); };
teacdabcontrols.prototype.captureBtnFavourite = function () { return this.startCapture('btn_favourite'); };
teacdabcontrols.prototype.captureBtnPause = function () { return this.startCapture('btn_pause'); };
teacdabcontrols.prototype.captureBtnRemoveFavourite = function () { return this.startCapture('btn_remove_favourite'); };
teacdabcontrols.prototype.captureBtnSleepTimer = function () { return this.startCapture('btn_sleep_timer'); };
teacdabcontrols.prototype.captureBtnCancelSleepTimer = function () { return this.startCapture('btn_cancel_sleep_timer'); };
teacdabcontrols.prototype.captureBtnDimmer = function () { return this.startCapture('btn_dimmer'); };
teacdabcontrols.prototype.captureBtnMainMenu = function () { return this.startCapture('btn_main_menu'); };
teacdabcontrols.prototype.captureBtnBack = function () { return this.startCapture('btn_back'); };

// Begin a capture session if one isn't already running. The session keeps
// the controls paused (via the flag file) until the user saves or goes
// idle, so button presses never reach the device while configuring.
// Returns false if the session could not be started.
teacdabcontrols.prototype.ensureCaptureSession = function () {
    const self = this;
    if (self._captureSession) { return true; }

    try {
        fs.writeFileSync(CAPTURE_FLAG_PATH, '');
    } catch (e) {
        self.logger.error('Teac DAB Controls - could not start capture: ' + e);
        self.commandRouter.pushToastMessage('error', 'Button Capture', 'Could not start capture mode.');
        return false;
    }
    try { fs.removeSync(CAPTURE_READING_PATH); } catch (e) {}
    try { fs.removeSync(CAPTURE_BASELINE_PATH); } catch (e) {}
    self._captureSession = { lastSeq: null };
    self._captureTimer = setInterval(function () { self.pollCapture(); }, CAPTURE_POLL_MS);
    return true;
};

teacdabcontrols.prototype.startCapture = function (targetKey) {
    const self = this;
    const label = CAPTURE_LABELS[targetKey] || targetKey;

    if (!self.ensureCaptureSession()) { return libQ.resolve(); }

    // (Re)target the session at the button the user just clicked.
    self._captureSession.target = targetKey;
    self._captureSession.label = label;
    self._captureSession.candidate = null;
    self._captureSession.deadline = Date.now() + CAPTURE_IDLE_TIMEOUT_MS;

    self.commandRouter.pushToastMessage('info', 'Button Capture',
        'Controls paused. Press the "' + label + '" button on the unit...');
    return libQ.resolve();
};

teacdabcontrols.prototype.pollCapture = function () {
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

    if (session.candidate.channel === ch && session.candidate.value === val) {
        const configValue = ch + ', ' + val;
        self.config.set(session.target, configValue);
        if (!self._capturedValues) { self._capturedValues = {}; }
        self._capturedValues[session.label] = configValue;
        self.commandRouter.pushToastMessage('success', 'Button Capture',
            'Captured "' + session.label + '" = ' + configValue + '. Configure another button, or click "Save & Restart Controls".');
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
teacdabcontrols.prototype.captureBaseResistance = function () {
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

teacdabcontrols.prototype.finishBaseResistanceCapture = function (session, startSeq) {
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
    self.config.set('btn_no_press_channel1', ch1 + ', ' + val1);
    self.config.set('btn_no_press_channel2', ch2 + ', ' + val2);
    self._capturedValues['No Press Channel 1'] = ch1 + ', ' + val1;
    self._capturedValues['No Press Channel 2'] = ch2 + ', ' + val2;

    self.commandRouter.pushToastMessage('success', 'Button Capture',
        'Captured base resistance: channel ' + ch1 + ' = ' + val1 + ', channel ' + ch2 + ' = ' + val2 +
        '. Configure another button, or click "Save & Restart Controls".');
};

teacdabcontrols.prototype.endCaptureSession = function () {
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
teacdabcontrols.prototype.saveCapture = function () {
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
    self.commandRouter.pushToastMessage('success', 'Button Capture',
        'Saved (' + summary + '). Restarting controls...');

    self._capturedValues = {};
    self.onRestart();
    return libQ.resolve();
};

// Plugin methods -----------------------------------------------------------------------------

// Run a systemctl command asynchronously so Volumio's event loop is never
// blocked while systemd works (a restart can wait on the old process to stop).
teacdabcontrols.prototype.systemctl = function (cmd, unit) {
    var self = this;

    if (!['start', 'stop', 'restart'].includes(cmd)) {
        return libQ.reject(new TypeError('Unknown systemd command: ' + cmd));
    }

    const defer = libQ.defer();
    exec(`/usr/bin/sudo /bin/systemctl ${cmd} ${unit} -q`, { uid: 1000, gid: 1000 }, function (error, stdout, stderr) {
        if (error) {
            self.logger.error(`Teac DAB Controls - unable to ${cmd} ${unit}: ${error}`);
            defer.reject(error);
            return;
        }
        if (stderr) {
            self.logger.error(`Teac DAB Controls - ${cmd} ${unit} stderr: ${stderr}`);
        }
        self.logger.info(`Teac DAB Controls - ${unit} ${cmd} complete`);
        defer.resolve();
    });

    return defer.promise;
};

teacdabcontrols.prototype.teacdabcontrolsServiceCmds = function (cmd) {
    return this.systemctl(cmd, 'teac-dab-controls.service');
};

teacdabcontrols.prototype.pigpiodServiceCmds = function (cmd) {
    return this.systemctl(cmd, 'pigpiod.service');
};
