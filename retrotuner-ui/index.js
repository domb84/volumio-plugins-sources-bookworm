'use strict';

var libQ = require('kew');
var fs = require('fs-extra');
var exec = require('child_process').exec;
var execSync = require('child_process').execSync;

// Dropped before a self-triggered restart, so the python service can tell one from a real shutdown.
var RESTART_MARKER_PATH = '/tmp/retrotuner-ui-restarting';

// SPI does nothing without this line, and only a reboot loads the driver.
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

// Ensure dtparam=spi=on is present. Returns 'present', 'added', or an error string.
// 'added' means the caller must ask for a reboot; until then every MCP3008 read is 0.
retrotunerui.prototype.ensureSpiInUserConfig = function () {
    const self = this;
    if (isSpiEnabledInUserConfig()) { return 'present'; }

    let contents = '';
    try {
        contents = fs.readFileSync(USERCONFIG_PATH, 'utf8');
    } catch (e) {
        contents = '';   // absent is fine -- writing creates it
    }

    // Without this our line glues onto the last one, silently breaking both settings.
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

// Audio tap for the meter, gated on the asound config so it is never spliced in by accident.
var AUDIO_TAP_CONF = 'rt_in.rt_out.2.conf';
var AUDIO_TAP_FIFO = '/tmp/retrotuner-audio.fifo';

// Rewritten in place from the settings page rather than templated, so its notes survive.
var CAVA_CONF = 'cava/retrotuner-cava.conf';

// Replace one key inside one [section], or null if it is not there. Section-aware
// because "channels" means different things under [input] and [output].
function replaceInIniSection(contents, sectionName, key, value) {
    const lines = contents.split('\n');
    const keyLine = new RegExp('^\\s*' + key + '\\s*=');
    let inSection = false;
    let replaced = false;

    for (let i = 0; i < lines.length; i++) {
        const header = lines[i].match(/^\s*\[([^\]]+)\]/);
        if (header) {
            inSection = header[1].trim() === sectionName;
            continue;
        }
        if (inSection && keyLine.test(lines[i])) {
            lines[i] = key + ' = ' + value;
            replaced = true;
        }
    }

    return replaced ? lines.join('\n') : null;
}

// What each mode needs from cava; neither end scales anything. See the table in NOTES.md.
var METER_MODES = {
    mono:        { bars: 16, channels: 'mono',   range: 16, autosens: 1, sensitivity: 100 },
    stereo:      { bars: 16, channels: 'stereo', range: 16, autosens: 1, sensitivity: 100 },
    rows_edges:  { bars: 32, channels: 'stereo', range: 4,  autosens: 1, sensitivity: 100 },
    rows_centre: { bars: 32, channels: 'stereo', range: 4,  autosens: 1, sensitivity: 100 },
    // The only mode with autosens off: it normalises a quiet passage up to look
    // like a loud one, which is the one thing a meter must not do.
    vu:          { bars: 32, channels: 'stereo', range: 80, autosens: 0, sensitivity: 100 }
};

// Next to METER_MODES so they cannot drift. A select renders blank until getUIConfig labels it.
var METER_MODE_LABELS = {
    mono:        'Mono - 16 bands, full height',
    stereo:      'Stereo mirrored - 8 bands per channel',
    rows_edges:  'Stereo rows - grow in from the edges',
    rows_centre: 'Stereo rows - grow out from the centre',
    vu:          'VU meters - one level bar per channel, with peak hold'
};

// The display is 16 columns; boot and screensaver text is trimmed to it on save.
var LCD_COLUMNS = 16;

// Effect ids must match includes/effects.py, labels must match UIConfig.json.
var BOOT_EFFECT_LABELS = {
    none:       'None - straight to the menu',
    splitflap:  'Split-flap - characters roll and settle',
    post:       'Power-on self test - all segments, then dissolve',
    wipe:       'Wipe - a curtain sweeps across and back',
    typewriter: 'Typewriter - characters land one at a time',
    slide:      'Slide-in - the rows arrive, hold, then leave again',
    tease:      'Meter tease - bars rise, collapse, reveal the text',
    centre:     'Centre-out reveal - curtains part from the middle'
};
var SCREENSAVER_EFFECT_LABELS = {
    none:    'None - leave the menu on screen',
    wave:    'Travelling wave - a sine scrolls across',
    bounce:  'Bouncing text - drifts around the panel',
    vu:      'VU meters - two bars with peak hold',
    scanner: 'Scanner - a block sweeps back and forth',
    rain:    'Data rain - characters cascade across'
};
var SCREENSAVER_TIMEOUT_LABELS = {
    30: '30 seconds', 60: '1 minute', 120: '2 minutes',
    300: '5 minutes', 600: '10 minutes', 1800: '30 minutes'
};

// String-valued settings and the exact set each may hold; saveOptions validates against this.
var STRING_SETTINGS = {
    meter_mode: Object.keys(METER_MODES),
    boot_effect: Object.keys(BOOT_EFFECT_LABELS),
    screensaver_effect: Object.keys(SCREENSAVER_EFFECT_LABELS)
};

// Free text rather than a fixed set, so isValid cannot check them against anything.
var TEXT_SETTINGS = ['boot_line1', 'boot_line2', 'screensaver_line1'];

retrotunerui.prototype.meterModeLabel = function (mode) {
    return METER_MODE_LABELS[mode] || METER_MODE_LABELS.mono;
};

retrotunerui.prototype.effectLabel = function (labels, id, fallback) {
    return labels[id] || labels[fallback];
};

// Written together because cava only reads them at startup, so they share one restart.
retrotunerui.prototype.applyCavaSettings = function () {
    const self = this;
    const conf = __dirname + '/' + CAVA_CONF;

    const rate = parseInt(self.config.get('meter_framerate', 60), 10) || 60;
    const mode = METER_MODES[self.config.get('meter_mode', 'mono')] || METER_MODES.mono;
    const wanted = [
        ['general', 'framerate', rate],
        ['general', 'bars', mode.bars],
        ['general', 'autosens', mode.autosens],
        ['general', 'sensitivity', mode.sensitivity],
        ['output', 'channels', mode.channels],
        ['output', 'ascii_max_range', mode.range]
    ];

    try {
        let contents = fs.readFileSync(conf, 'utf8');

        for (const [section, key, value] of wanted) {
            const updated = replaceInIniSection(contents, section, key, value);
            if (updated === null) {
                // Renamed or removed. Silence would leave cava on its old value,
                // analysing differently from what the settings page claims.
                self.logger.error('RetroTuner UI - no ' + key + ' under [' + section + '] in ' +
                    conf + '; cava settings unchanged');
                return false;
            }
            contents = updated;
        }

        fs.writeFileSync(conf, contents, 'utf8');
        self.logger.info('RetroTuner UI - cava set to ' + rate + ' fps, ' + mode.bars +
            ' bars, ' + mode.channels + ', range ' + mode.range);
        return true;
    } catch (e) {
        self.logger.error('RetroTuner UI - could not write cava settings: ' + e);
        return false;
    }
};

retrotunerui.prototype.setupAudioTap = function () {
    const self = this;
    const conf = __dirname + '/asound/' + AUDIO_TAP_CONF;

    if (!fs.existsSync(conf)) {
        // Logged, or "no audio tap" on the display looks identical to this never running.
        self.logger.info('RetroTuner UI - audio tap not enabled (no ' + conf + ')');
        return false;
    }

    // volumiofifo writes the fifo but does not create it. Reuse an existing one:
    // recreating leaves cava reading the old, unlinked inode no writer can reach.
    try {
        let usable = false;
        try {
            usable = fs.statSync(AUDIO_TAP_FIFO).isFIFO();
        } catch (e) {
            usable = false;      // missing, or not stat-able
        }

        if (!usable) {
            fs.removeSync(AUDIO_TAP_FIFO);   // a stale regular file would wedge it
            // As the volumio user, not root: this process runs as root and cava does not.
            execSync('/usr/bin/mkfifo -m 646 ' + AUDIO_TAP_FIFO, { uid: 1000, gid: 1000 });
            self.logger.info('RetroTuner UI - created audio tap fifo ' + AUDIO_TAP_FIFO);
        }
    } catch (e) {
        self.logger.error('RetroTuner UI - could not prepare audio tap fifo: ' + e);
    }

    // Volumio only folds plugin asound/ files into /etc/asound.conf when asked.
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

    // A fresh install never saves settings, so nothing else would add the boot
    // parameter. Logged, not a modal: there may be no browser session at boot.
    if (Boolean(self.config.get('spi')) && self.ensureSpiInUserConfig() === 'added') {
        self.logger.warn('RetroTuner UI - SPI enabled in ' + USERCONFIG_PATH +
            '; a reboot is required before the MCP3008 can be read');
    }

    // meter_stereo (switch) became meter_mode (select): carry the choice across once, then drop it.
    if (self.config.has('meter_stereo')) {
        if (Boolean(self.config.get('meter_stereo')) &&
            String(self.config.get('meter_mode', 'mono')) === 'mono') {
            self.config.set('meter_mode', 'stereo');
            self.logger.info('RetroTuner UI - migrated meter_stereo to meter_mode = stereo');
        }
        self.config.delete('meter_stereo');
    }

    // An update reinstalls cava's config with shipped defaults; reapply or it diverges from config.json.
    if (tapEnabled) {
        self.applyCavaSettings();
    }

    // Start pigpiod first (the python controls connect to it), then our service.
    return self.pigpiodServiceCmds('start')
        .then(function () {
            // Only with the tap installed: no tap and cava reads nothing, tap and no cava stalls playback.
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

    // Mark this as our own restart, so the controls do not show the shutdown screen.
    try { fs.writeFileSync(RESTART_MARKER_PATH, String(Date.now())); }
    catch (e) { self.logger.error('RetroTuner UI - could not write restart marker: ' + e); }

    // 'start' not 'restart' for pigpiod: a restart races the pigpio reconnect and kills the encoder.
    return self.pigpiodServiceCmds('start')
        .then(function () { return self.retrotuneruiServiceCmds('restart'); })
        .fail(function (e) { self.logger.error('RetroTuner UI - error restarting: ' + e); });
};

// "Restart Now" on the SPI-mode-changed modal, via callMethod. Only a reboot re-probes the pin mux.
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
            // By id, not numeric index: reordering sections has silently broken this page before.
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

            // Action buttons store no values, but each "Configure" label shows its
            // current mapping, so the user can see what is set without opening Advanced.
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

            // A select carries its own label; a bare number leaves the control blank.
            const meterRate = parseInt(self.config.get('meter_framerate', 60), 10) || 60;
            setValue(section('level_meter'), 'meter_framerate',
                     { value: meterRate, label: meterRate + ' fps' });
            const meterMode = String(self.config.get('meter_mode', 'mono'));
            setValue(section('level_meter'), 'meter_mode',
                     { value: meterMode, label: self.meterModeLabel(meterMode) });

            // Defaults match effects.py, so an older config shows what it will actually do.
            const screen = section('screen_effects');
            const bootEffect = String(self.config.get('boot_effect', 'splitflap'));
            setValue(screen, 'boot_effect',
                     { value: bootEffect,
                       label: self.effectLabel(BOOT_EFFECT_LABELS, bootEffect, 'splitflap') });
            setValue(screen, 'boot_line1', self.config.get('boot_line1', ''));
            setValue(screen, 'boot_line2', self.config.get('boot_line2', ''));
            const saverEffect = String(self.config.get('screensaver_effect', 'wave'));
            setValue(screen, 'screensaver_effect',
                     { value: saverEffect,
                       label: self.effectLabel(SCREENSAVER_EFFECT_LABELS, saverEffect, 'wave') });
            setValue(screen, 'screensaver_line1', self.config.get('screensaver_line1', ''));
            const saverTimeout = parseInt(self.config.get('screensaver_timeout', 120), 10) || 120;
            setValue(screen, 'screensaver_timeout',
                     { value: saverTimeout,
                       label: SCREENSAVER_TIMEOUT_LABELS[saverTimeout] || (saverTimeout + ' seconds') });

            // Also new -- default matches apply_log_level()'s own fallback in index.py.
            setValue(section('diagnostics'), 'debug_mode', self.config.get('debug_mode', false));

            const encoder = section('encoder');
            setValue(encoder, 'rot_enc_A', self.config.get('rot_enc_A'));
            setValue(encoder, 'rot_enc_B', self.config.get('rot_enc_B'));
            // Default matches menu_manager's fallback for a config predating it (skip stays off).
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
            // Without defaults an old config leaves the field blank and Save submits empty. Match controls.py.
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

    // Captured before the save loop overwrites it. Flagged separately from the usual
    // restart: only a boot-time driver probe reapplies the pin mux.
    const spiModeChanged = data.hasOwnProperty('spi') &&
        Boolean(data.spi) !== Boolean(self.config.get('spi'));

    // Switches, numbers and the string selects above. The capture flow writes button mappings directly.
    function isValid(key, value) {
        // A select posts a string; each such field declares the exact set it may hold,
        // rather than accepting any string and letting a typo reach cava.
        if (STRING_SETTINGS.hasOwnProperty(key)) {
            return STRING_SETTINGS[key].indexOf(value) !== -1;
        }
        // Free text: anything goes, already trimmed to the display width below.
        if (TEXT_SETTINGS.indexOf(key) !== -1) {
            return typeof value === 'string';
        }
        if (typeof value === 'boolean') {
            return true;
        }
        return !isNaN(parseFloat(value)) && isFinite(value);
    }

    // A select posts {value, label}; flatten it before the validation loop, which
    // only understands numbers and booleans and would reject the whole field.
    for (const key of ['meter_framerate', 'meter_mode', 'boot_effect',
                       'screensaver_effect', 'screensaver_timeout']) {
        if (data.hasOwnProperty(key) && data[key] !== null && typeof data[key] === 'object') {
            data[key] = data[key].value;
        }
    }

    // Trimmed here rather than at render time, so the config never holds text the panel cannot show.
    for (const key of TEXT_SETTINGS) {
        if (data.hasOwnProperty(key)) {
            const text = (data[key] === null || data[key] === undefined) ? '' : String(data[key]);
            data[key] = text.slice(0, LCD_COLUMNS);
        }
    }

    // Captured before the save loop overwrites them; only a real change is worth bouncing cava.
    const meterRateChanged = data.hasOwnProperty('meter_framerate') &&
        parseInt(data.meter_framerate, 10) !== parseInt(self.config.get('meter_framerate', 60), 10);
    const meterModeChanged = data.hasOwnProperty('meter_mode') &&
        String(data.meter_mode) !== String(self.config.get('meter_mode', 'mono'));
    const meterChanged = meterRateChanged || meterModeChanged;

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
            if (isValid(key, value)) {
                self.config.set(key, value);
            } else {
                self.logger.error(`${value} is not a valid number or boolean. Not saving ${key}.`);
                this.commandRouter.pushToastMessage('fail', ("RetroTuner UI"), (`${value} is not a valid number or boolean. Not saving ${key}.`));
            }
        }
    }
    
    self.logger.info('RetroTuner UI - settings saved');
    this.commandRouter.pushToastMessage('success', ("RetroTuner UI"), this.commandRouter.getI18nString("COMMON.CONFIGURATION_UPDATE_DESCRIPTION"));

    // Checked whenever SPI is on, not only when the switch changes: SPI is the
    // default, so a fresh install would never get the line added at all.
    const bootConfig = Boolean(self.config.get('spi')) ? self.ensureSpiInUserConfig() : 'present';
    const rebootPending = spiModeChanged || bootConfig === 'added';

    if (bootConfig !== 'present' && bootConfig !== 'added') {
        // Write failed (read-only /boot, permissions); a reboot would not help, so offer no button.
        self.commandRouter.broadcastMessage('openModal', {
            title: 'SPI Mode Enabled -- Boot Config Not Writable',
            message: 'SPI mode is enabled, but "' + SPI_USERCONFIG_LINE + '" could not be added to ' +
                USERCONFIG_PATH + ' (' + bootConfig + '). Add that line manually via SSH and reboot, ' +
                'otherwise button reads will come back as a constant 0.',
            size: 'lg',
            buttons: [{ name: 'OK', class: 'btn btn-default' }]
        });
    } else if (rebootPending) {
        // Same shape as Volumio's "I2S DAC enabled" prompt - a plugin restart cannot apply either change.
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

    // cava only reads these at startup, so it has to be bounced - which onRestart
    // never does, an unread tap stalling playback. Gated on a real change for that reason.
    if (meterChanged && !rebootPending) {
        if (self.applyCavaSettings()) {
            self.commandRouter.pushToastMessage('info', 'RetroTuner UI',
                'Restarting the audio analyser. Playback may skip briefly.');
            self.cavaServiceCmds('try-restart').fail(function (e) {
                self.logger.error('RetroTuner UI - could not restart cava: ' + e);
            });
        }
    }

    const noConflicts = self._checkButtonConflicts();  // always run: pushes its own toast on conflict

    if (rebootPending) {
        // Only a reboot re-probes the pin mux; restarting now opens a device that is not there yet.
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

// Raw ADC readings never repeat exactly, so a capture is confirmed by tolerance and
// stored as a band around the midpoint of the two readings.
var ADC_MAX = 1023;
// Sized against the measured 69-141 gap, wide enough for contact resistance. See NOTES.md.
var CAPTURE_CONFIRM_TOLERANCE = 15;  // counts two presses may differ by and still confirm
var CAPTURE_BAND_HALF_WIDTH = 25;    // half-width of the band written to the config

// An unreachable MCP3008 returns 0 on every channel, so no band may claim it.
var MIN_BAND_VALUE = 1;

// One definition, so a press and a resting value are banded alike and the clamps exist once.
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

// Push getUIConfig() to any open settings page, so a value changed here shows without a reload.
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

// Clear the button selected for capture. Staged with capture, so one "Save & Restart
// Controls" applies both: choose via "Configure", then "Clear Selected Button".
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

// Begin a capture session, pausing the controls until save or idle. False if it could not start.
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

    // One new seq is one physical press; python filters resting values and releases.
    const ch = reading.channel;
    const val = reading.value;

    if (session.candidate == null) {
        session.candidate = { channel: ch, value: val };
        self.commandRouter.pushToastMessage('info', 'Button Capture',
            'Read channel ' + ch + ', value ' + val + '. Press "' + session.label + '" again to confirm.');
        return;
    }

    if (session.candidate.channel === ch && Math.abs(session.candidate.value - val) <= CAPTURE_CONFIRM_TOLERANCE) {
        // A band around the midpoint of the two presses: a raw reading is never identical twice.
        const centre = Math.round((session.candidate.value + val) / 2);
        const configValue = ch + ', ' + bandAround(centre);
        if (!self._capturedValues) { self._capturedValues = {}; }

        // Auto-reassign: clear any other action holding this value, so the button triggers only this one.
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

// Resting value of both ADC channels, measured by python at capture start.
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

// Async, so Volumio's event loop never blocks while systemd waits on the old process to stop.
retrotunerui.prototype.systemctl = function (cmd, unit) {
    var self = this;

    // try-restart only bounces a running unit, so this never starts cava on a box with no tap.
    if (!['start', 'stop', 'restart', 'try-restart'].includes(cmd)) {
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

// Must run for as long as the tap is in the ALSA chain, not just while the meter is on screen.
retrotunerui.prototype.cavaServiceCmds = function (cmd) {
    return this.systemctl(cmd, 'retrotuner-cava.service');
};
