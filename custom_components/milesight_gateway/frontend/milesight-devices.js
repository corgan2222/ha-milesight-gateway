// The LoRaWAN device page: pick devices, set what their models share, look at
// the bytes, send, and follow each command. Data and sending go through the
// milesight_gateway/* websocket commands (administrators only).
//
// The page is drawn in parts (list, settings, log, import), each on its own,
// so the refresh every ten seconds neither scrolls the list back nor takes
// the text out of a field being typed in.

const TEXT = {
  en: {
    title: "LoRaWAN devices",
    counts: (n, on, off) => `${n} devices · ${on} online · ${off} offline`,
    search: "Search name, type, devEUI",
    allGateways: "All gateways", allTypes: "All types", allFloors: "All floors", allRooms: "All rooms",
    clear: "Clear", pickAll: "Select all online devices shown",
    selected: (n) => `${n} selected`,
    device: "Device", model: "Type", gateway: "Gateway", status: "Status", lastSeen: "Last seen",
    battery: "Battery", signal: "Signal", loss: "Loss", command: "Last command",
    via: "via", openGateway: "Open the gateway's device page", toIntegration: "Open the integration",
    tally: { title: "Last send", sent: "sent", waiting: "waiting", acked: "ACK", confirmed: "confirmed", unconfirmed: "sent, no answer", failed: "failed", open: "open in total", showOnly: "Show only these", showAll: "Show all again" },
    interval: "Interval", measured: "measured", nextIn: (x) => `next uplink in ${x}`, overdue: "uplink overdue",
    openDevice: "Open the device page",
    paths: { mqtt: "MQTT", rest: "Network server (REST)", "mqtt+rest": "MQTT, settings by REST" },
    online: "Online", offline: "Offline", never: "never",
    quality: { excellent: "Excellent", good: "Good", fair: "Fair", poor: "Poor" },
    settings: "Settings", general: "General", commands: "Commands",
    partial: "Only some of the selected devices",
    settingsFor: (n) => `for ${n} ${n === 1 ? "device" : "devices"}`,
    findSetting: "Find a setting",
    on: "On", off: "Off", unchanged: "Take the change back", choose: "do not change",
    noneSelected: "Pick devices in the list to change their settings.",
    noCommon: "The selected types have no settings.",
    mixed: "differs between the devices", now: "now", example: "example",
    src: { device: "reported by the device", ack: "acknowledged", sent: "sent, not confirmed", current: "current" },
    help: "What is this?", field: "Field", range: "Range", useDefault: "Use the example value", types: "Types",
    summary: (c, d) => `${c} ${c === 1 ? "change" : "changes"} for ${d} ${d === 1 ? "device" : "devices"}`,
    nothing: "Nothing changed yet.",
    reset: "Reset", preview: "Preview", send: "Send", sending: "Sending …",
    bytes: "Bytes", result: "Result", sentOk: "Queued", noSend: "Takes no settings",
    log: "Log", time: "Time", attempts: "Tries", emptyLog: "No commands sent yet.",
    states: { pending: "Waiting for the next uplink", retrying: "Sending again", transmitted: "Delivered, waiting for ACK",
      acknowledged: "Acknowledged", confirmed: "Confirmed by device", differs: "Device reports otherwise",
      sent: "Sent", failed: "Failed" },
    join: "Joined the network",
    ago: (text) => `${text} ago`,
    import: "Add devices", pickGateway: "Gateway", application: "Application",
    profile: "Device profile", codec: "Payload codec", auto: "Built-in codec of the model",
    rows: "One device per line: devEUI; name; AppKey (optional, else the factory key)",
    doImport: "Create on the gateway", created: "Created", noCodec: "no codec found",
  },
  de: {
    title: "LoRaWAN-Geräte",
    counts: (n, on, off) => `${n} Geräte · ${on} online · ${off} offline`,
    search: "Name, Typ, devEUI suchen",
    allGateways: "Alle Gateways", allTypes: "Alle Typen", allFloors: "Alle Etagen", allRooms: "Alle Räume",
    clear: "Leeren", pickAll: "Alle angezeigten Online-Geräte wählen",
    selected: (n) => `${n} gewählt`,
    device: "Gerät", model: "Typ", gateway: "Gateway", status: "Status", lastSeen: "Zuletzt gesehen",
    battery: "Batterie", signal: "Signal", loss: "Verlust", command: "Letzter Befehl",
    via: "über", openGateway: "Geräteseite des Gateways öffnen", toIntegration: "Integration öffnen",
    tally: { title: "Letzter Sendevorgang", sent: "gesendet", waiting: "warten", acked: "ACK", confirmed: "bestätigt", unconfirmed: "gesendet, ohne Antwort", failed: "fehlgeschlagen", open: "offen insgesamt", showOnly: "Nur diese zeigen", showAll: "Wieder alle zeigen" },
    interval: "Intervall", measured: "gemessen", nextIn: (x) => `nächster Uplink in ${x}`, overdue: "Uplink überfällig",
    openDevice: "Geräteseite öffnen",
    paths: { mqtt: "MQTT", rest: "Network Server (REST)", "mqtt+rest": "MQTT, Senden per REST" },
    online: "Online", offline: "Offline", never: "nie",
    quality: { excellent: "Sehr gut", good: "Gut", fair: "Mäßig", poor: "Schlecht" },
    settings: "Einstellungen", general: "Allgemein", commands: "Befehle",
    partial: "Nur bei einem Teil der Auswahl",
    settingsFor: (n) => `für ${n} ${n === 1 ? "Gerät" : "Geräte"}`,
    findSetting: "Einstellung suchen",
    on: "An", off: "Aus", unchanged: "Änderung zurücknehmen", choose: "nicht ändern",
    noneSelected: "Geräte in der Liste wählen, um ihre Einstellungen zu ändern.",
    noCommon: "Die gewählten Typen haben keine Einstellungen.",
    mixed: "je Gerät unterschiedlich", now: "aktuell", example: "Beispiel",
    src: { device: "vom Gerät gemeldet", ack: "bestätigt (ACK)", sent: "gesendet, unbestätigt", current: "aktuell" },
    help: "Was ist das?", field: "Feld", range: "Bereich", useDefault: "Beispielwert übernehmen", types: "Typen",
    summary: (c, d) => `${c} ${c === 1 ? "Änderung" : "Änderungen"} für ${d} ${d === 1 ? "Gerät" : "Geräte"}`,
    nothing: "Noch nichts geändert.",
    reset: "Zurücksetzen", preview: "Vorschau", send: "Senden", sending: "Sende …",
    bytes: "Bytes", result: "Ergebnis", sentOk: "Eingereiht", noSend: "Nimmt keine Einstellungen",
    log: "Protokoll", time: "Zeit", attempts: "Versuche", emptyLog: "Noch keine Befehle gesendet.",
    states: { pending: "Wartet auf nächsten Uplink", retrying: "Wird wiederholt", transmitted: "Zugestellt, wartet auf ACK",
      acknowledged: "Bestätigt (ACK)", confirmed: "Vom Gerät bestätigt", differs: "Gerät meldet abweichend",
      sent: "Gesendet", failed: "Fehlgeschlagen" },
    join: "Neu gejoint",
    ago: (text) => `vor ${text}`,
    import: "Geräte anlegen", pickGateway: "Gateway", application: "Anwendung",
    profile: "Geräteprofil", codec: "Payload-Codec", auto: "Eingebauter Codec des Modells",
    rows: "Ein Gerät je Zeile: devEUI; Name; AppKey (optional, sonst der Werksschlüssel)",
    doImport: "Auf dem Gateway anlegen", created: "Angelegt", noCodec: "kein Codec gefunden",
  },
};

// What a setting does, by field ID or by the last part of it. Milesight's
// catalog names the fields but does not explain them.
const HELP = {
  en: {
    report_interval: "How often the device sends its readings. Shorter means fresher values, more radio traffic and a shorter battery life.",
    reporting_interval: "How often the device sends its readings. Shorter means fresher values, more radio traffic and a shorter battery life.",
    collection_interval: "How often the sensor measures. The values go out with the report interval, or at once on an alarm.",
    adr_enable: "Adaptive Data Rate: the network server adapts data rate and transmit power to the radio link. Recommended for devices that stay in place; it saves battery.",
    d2d_enable: "Milesight D2D: the device controls other Milesight devices directly by radio, without the gateway.",
    history_enable: "The device stores its readings, so they can be fetched later.",
    retransmit_enable: "Data retransmission: after losing the network, the device sends the readings it could not deliver once it is connected again.",
    retransmit_interval: "Time between two packets while the device retransmits readings it could not deliver.",
    resend_interval: "Time between two packets while stored readings are fetched from the device.",
    lora_port: "The LoRaWAN port (FPort) the device sends on; Milesight uses 85. Change it only if the receiving side expects the same port, or no values arrive any more.",
    time_zone: "Time zone of the device's clock, for schedules and stored readings.",
    time_sync_enable: "The device sets its clock by the network server.",
    report_type: "Whether readings go out only with the report interval (period) or at once when they change (immediately).",
    target_temperature: "The temperature the thermostat regulates to.",
    temperature_tolerance: "How far the room may deviate from the target before the valve moves.",
    led_indicator_enable: "Switches the status LED on the device on or off.",
    led_indicator_mode: "When the status LED lights up.",
    buzzer_enable: "Switches the buzzer on the device on or off.",
    reboot: "Restarts the device.",
    report_status: "Asks for a status report: the device sends its versions, serial number and current settings.",
    query_device_status: "Asks for a status report: the device sends its versions, serial number and current settings.",
    query_status: "Asks for a status report: the device sends its versions, serial number and current settings.",
    sync_time: "Sets the device's clock.",
    stop_buzzer: "Silences a sounding buzzer.",
    clear_history: "Deletes the readings stored on the device.",
    stop_transmit: "Stops a running transmission of stored readings.",
    timestamp: "Sets the device's clock to this time.",
    d2d_key: "The key Milesight devices share to talk to each other by D2D. It has to be the same on both sides.",
    temperature_unit: "Whether the device shows and reports temperatures in Celsius or Fahrenheit.",
    alarm_report_counts: "How many times the device repeats an alarm report.",
    alarm_report_interval: "Time between two repeated alarm reports.",
    reset: "Resets the device to its factory settings.",
    reset_battery: "Tells the device that it got a new battery, so it counts the level from full again.",
    clear_report_schedule: "Deletes the stored schedule for reports.",
    valve_opening: "Sets how far the valve is open, in percent.",
    valve_calibration: "Lets the thermostat measure the valve's travel again. Needed after mounting it on another valve.",
    offline_control_mode: "What the thermostat does when it loses the network.",
    outside_temperature: "Hands the thermostat a temperature measured elsewhere in the room, to regulate by.",
    vacant_reporting_interval: "How often the device reports while nobody is there.",
    vacancy_reporting_interval: "How often the device reports while nobody is there.",
    installation_height: "The height the sensor is mounted at; it scales its detection by it.",
    tx_power_level: "The transmit power. More reaches further and costs battery.",
    data_rate: "The LoRaWAN data rate (spreading factor). A lower one reaches further, takes longer on air and costs battery. With ADR on, the network sets it.",
    lorawan_version: "The LoRaWAN version the device speaks. It has to match the device's entry on the gateway.",
    lora_join_mode: "How the device joins the network: OTAA with a join procedure, or ABP with fixed keys.",
  },
  de: {
    report_interval: "Wie oft das Gerät seine Messwerte sendet. Kürzer heißt aktuellere Werte, mehr Funkverkehr und kürzere Batterielaufzeit.",
    reporting_interval: "Wie oft das Gerät seine Messwerte sendet. Kürzer heißt aktuellere Werte, mehr Funkverkehr und kürzere Batterielaufzeit.",
    collection_interval: "Wie oft der Sensor misst. Gesendet wird mit dem Sendeintervall oder sofort bei einem Alarm.",
    adr_enable: "Adaptive Data Rate: der Network Server passt Datenrate und Sendeleistung an die Funkstrecke an. Für fest montierte Geräte empfohlen; das spart Batterie.",
    d2d_enable: "Milesight D2D: das Gerät steuert andere Milesight-Geräte direkt per Funk, ohne Gateway.",
    history_enable: "Das Gerät speichert seine Messwerte, damit sie später abgerufen werden können.",
    retransmit_enable: "Datenwiederholung: verliert das Gerät das Netz, sendet es die nicht zugestellten Messwerte nach, sobald es wieder verbunden ist.",
    retransmit_interval: "Abstand zwischen zwei Paketen, während das Gerät nicht zugestellte Messwerte nachsendet.",
    resend_interval: "Abstand zwischen zwei Paketen, während gespeicherte Messwerte vom Gerät abgerufen werden.",
    lora_port: "Der LoRaWAN-Port (FPort), auf dem das Gerät sendet; Milesight nutzt 85. Nur ändern, wenn die Gegenseite denselben Port erwartet, sonst kommen keine Werte mehr an.",
    time_zone: "Zeitzone der Geräteuhr, für Zeitpläne und gespeicherte Messwerte.",
    time_sync_enable: "Das Gerät stellt seine Uhr nach dem Network Server.",
    report_type: "Ob Messwerte nur mit dem Sendeintervall gesendet werden (Periodisch) oder sofort bei einer Änderung (Sofort).",
    target_temperature: "Die Temperatur, auf die das Thermostat regelt.",
    temperature_tolerance: "Wie weit der Raum vom Sollwert abweichen darf, bevor das Ventil regelt.",
    led_indicator_enable: "Schaltet die Status-LED am Gerät ein oder aus.",
    led_indicator_mode: "Wann die Status-LED leuchtet.",
    buzzer_enable: "Schaltet den Summer am Gerät ein oder aus.",
    reboot: "Startet das Gerät neu.",
    report_status: "Fordert einen Statusbericht an: das Gerät sendet Versionen, Seriennummer und aktuelle Einstellungen.",
    query_device_status: "Fordert einen Statusbericht an: das Gerät sendet Versionen, Seriennummer und aktuelle Einstellungen.",
    query_status: "Fordert einen Statusbericht an: das Gerät sendet Versionen, Seriennummer und aktuelle Einstellungen.",
    sync_time: "Stellt die Uhr des Geräts.",
    stop_buzzer: "Schaltet einen tönenden Summer stumm.",
    clear_history: "Löscht die auf dem Gerät gespeicherten Messwerte.",
    stop_transmit: "Bricht eine laufende Übertragung gespeicherter Messwerte ab.",
    timestamp: "Stellt die Uhr des Geräts auf diese Zeit.",
    d2d_key: "Der Schlüssel, mit dem Milesight-Geräte per D2D miteinander sprechen. Er muss auf beiden Seiten gleich sein.",
    temperature_unit: "Ob das Gerät Temperaturen in Celsius oder Fahrenheit zeigt und meldet.",
    alarm_report_counts: "Wie oft das Gerät eine Alarmmeldung wiederholt.",
    alarm_report_interval: "Abstand zwischen zwei wiederholten Alarmmeldungen.",
    reset: "Setzt das Gerät auf die Werkseinstellungen zurück.",
    reset_battery: "Sagt dem Gerät, dass es eine neue Batterie hat; es zählt den Füllstand wieder von voll.",
    clear_report_schedule: "Löscht den gespeicherten Zeitplan für Meldungen.",
    valve_opening: "Stellt ein, wie weit das Ventil offen ist, in Prozent.",
    valve_calibration: "Lässt das Thermostat den Ventilhub neu vermessen. Nötig nach der Montage auf einem anderen Ventil.",
    offline_control_mode: "Was das Thermostat tut, wenn es das Netz verliert.",
    outside_temperature: "Gibt dem Thermostat eine Temperatur vor, die an anderer Stelle im Raum gemessen wurde, zum Regeln.",
    vacant_reporting_interval: "Wie oft das Gerät meldet, solange niemand da ist.",
    vacancy_reporting_interval: "Wie oft das Gerät meldet, solange niemand da ist.",
    installation_height: "Die Höhe, in der der Sensor montiert ist; danach richtet er seine Erfassung.",
    tx_power_level: "Die Sendeleistung. Mehr reicht weiter und kostet Batterie.",
    data_rate: "Die LoRaWAN-Datenrate (Spreading Factor). Eine niedrigere reicht weiter, braucht länger in der Luft und kostet Batterie. Mit ADR stellt das Netz sie ein.",
    lorawan_version: "Die LoRaWAN-Version, die das Gerät spricht. Sie muss zum Eintrag des Geräts auf dem Gateway passen.",
    lora_join_mode: "Wie das Gerät dem Netz beitritt: OTAA mit Join-Verfahren oder ABP mit festen Schlüsseln.",
  },
};

// What a field's ID says it measures, for the texts below: [pattern, en, de].
const QUANTITIES = [
  [/temperature|celsius|fahrenheit/, "temperature", "Temperatur"], [/humidity/, "humidity", "Luftfeuchte"],
  [/co2/, "CO₂", "CO₂"], [/tvoc/, "TVOC", "TVOC"], [/pm1_0|pm2_5|pm10/, "particulate matter", "Feinstaub"],
  [/illuminance|light/, "illuminance", "Helligkeit"], [/pressure/, "pressure", "Druck"],
  [/distance|level|tank|bin_mode/, "distance", "Abstand"], [/current/, "current", "Strom"], [/voltage/, "voltage", "Spannung"],
  [/leak/, "leakage", "Leckage"], [/people|occup/, "people count", "Personenzahl"], [/angle|tilt/, "tilt", "Neigung"],
  [/moisture/, "soil moisture", "Bodenfeuchte"], [/depth/, "depth", "Tiefe"], [/electricity|conductivity/, "conductivity", "Leitfähigkeit"],
  [/magnet|door/, "door contact", "Türkontakt"], [/water_flow|water_outage|flow/, "flow", "Durchfluss"], [/vaping/, "vaping index", "Vaping-Index"],
  [/h2s/, "H₂S", "H₂S"], [/nh3/, "NH₃", "NH₃"], [/power/, "power", "Leistung"], [/geofence/, "geofence", "Geofence"],
  [/battery/, "battery", "Batterie"], [/altitude/, "altitude", "Höhe über dem Meer"],
];

// What a group of fields is about, by the whole field ID; the first match
// counts. A text takes the quantity (q) the ID names, if any.
const TOPICS = [
  [/mutation/, (q) => `${q ? `${q} a` : "A"}larm on an abrupt change of the reading.`, (q) => `${q ? `${q}-` : ""}Alarm bei sprunghafter Änderung des Messwerts.`],
  [/alarm/, (q) => `${q ? `${q} a` : "A"}larm: the device reports at once when the condition is met.`, (q) => `${q ? `${q}-` : ""}Alarm: das Gerät meldet sofort, wenn die Bedingung erfüllt ist.`],
  [/calibration/, (q) => `Calibration${q ? ` (${q})` : ""}: corrects what the sensor measures.`, (q) => `Kalibrierung${q ? ` (${q})` : ""}: korrigiert, was der Sensor misst.`],
  [/^dst_config|daylight_saving/, () => "Daylight saving time: when the device changes its clock.", () => "Sommerzeit: wann das Gerät seine Uhr umstellt."],
  [/hibernate|dormant|buzzer_sleep/, () => "Quiet time: in this period the device rests or works less, which saves battery.", () => "Ruhezeit: in diesem Zeitraum ruht das Gerät oder arbeitet eingeschränkt; das spart Batterie."],
  [/child(ren)?_lock|button_lock|unlock/, () => "Button lock: locks the buttons on the device against changes.", () => "Tastensperre: sperrt die Tasten am Gerät gegen Verstellen."],
  [/window/, () => "Open window detection: when the temperature drops fast, the thermostat closes the valve.", () => "Fenster-offen-Erkennung: fällt die Temperatur schnell, schließt das Thermostat das Ventil."],
  [/freez/, () => "Frost protection: below this temperature the device heats, even when it is off otherwise.", () => "Frostschutz: unter dieser Temperatur heizt das Gerät, auch wenn es sonst aus ist."],
  [/heating_date|heating_period/, () => "Heating period: the time of the year the thermostat regulates in.", () => "Heizperiode: der Zeitraum im Jahr, in dem das Thermostat regelt."],
  [/temperature_range|setpoint_range/, () => "Limits for the target temperature that can be set on the device.", () => "Grenzen für die Solltemperatur, die sich am Gerät einstellen lässt."],
  [/schedule|_task|plan/, () => "Schedule: what the device does by itself, and when.", () => "Zeitplan: was das Gerät von selbst tut und wann."],
  [/screen|display|backlight|template|block_visible|text_|qrcode|image/, () => "The display on the device: what it shows and how.", () => "Anzeige am Gerät: was sie zeigt und wie."],
  [/^d2d/, () => "Milesight D2D: a direct radio link between Milesight devices, without the gateway.", () => "Milesight D2D: direkte Funkverbindung zwischen Milesight-Geräten, ohne Gateway."],
  [/install/, () => "Mounting: details of the place, which the sensor adapts its measurement to.", () => "Montage: Angaben zum Einbauort, nach denen der Sensor seine Messung richtet."],
  [/histor|retrieve|storage|stop_transmit/, () => "Stored readings: keeping, fetching or deleting them.", () => "Gespeicherte Messwerte: speichern, abrufen oder löschen."],
  [/rejoin|lora|rx2|tx_power|data_rate|multicast|class_|confirm_mode|ack_retry|adr|communicat|reconnect|heartbeat/, () => "LoRaWAN radio: values that do not fit the network cut the device off from the gateway.", () => "LoRaWAN-Funk: Werte, die nicht zum Netz passen, trennen das Gerät vom Gateway."],
  [/thermopile|pir|detect|radar|tof|region|sleep|fall|respirat|motion|static/, () => "Detection: how the sensor recognises presence or movement.", () => "Erfassung: wie der Sensor Anwesenheit oder Bewegung erkennt."],
  [/people|cumulative|counter|count|pulse/, () => "Counting: people or pulses the device adds up.", () => "Zählung: Personen oder Impulse, die das Gerät aufsummiert."],
  [/modbus|rs485|rs232|serial|interface|sdi|ai_|analog/, () => "Wired interfaces: connecting what is plugged into the device.", () => "Kabelgebundene Schnittstellen: Anbindung dessen, was am Gerät angeschlossen ist."],
  [/valve/, () => "Valve: its position and behaviour.", () => "Ventil: Stellung und Verhalten."],
  [/fan/, () => "Fan: its speed and behaviour.", () => "Lüfter: Stufe und Verhalten."],
  [/temp(erature)?_control|setpoint|target_temp|deadband|dead_zone|dehumid|energy_saving|auto_away|set_temperature|send_temperature|temperature_source|temperature_level/, () => "Temperature control: how the device regulates to the target temperature.", () => "Temperaturregelung: wie das Gerät auf die Solltemperatur regelt."],
  [/buzzer/, () => "The buzzer on the device.", () => "Summer am Gerät."],
  [/led/, () => "The status LED on the device.", () => "Status-LED am Gerät."],
  [/button/, () => "The buttons on the device.", () => "Tasten am Gerät."],
  [/ble|bluetooth|wifi|nfc/, () => "Short range radio, for setting the device up or locating it.", () => "Nahfunk, zum Einrichten des Geräts oder zur Ortung."],
  [/gnss|position|geofence/, () => "Positioning: how and when the device finds its place.", () => "Ortung: wie und wann das Gerät seinen Standort bestimmt."],
  [/gpio|digital|^di_|_di_|relay|switch|socket|wires|output|rule|system_status|card/, () => "Inputs and outputs: switching states and what follows from them.", () => "Ein- und Ausgänge: Schaltzustände und was daraus folgt."],
  [/power|energy|current|voltage|capacitor/, () => "Power: measuring or supplying it.", () => "Strom: Messung oder Versorgung."],
  [/report|interval|collect/, () => "Reporting: when the device measures and sends, and what.", () => "Senden: wann das Gerät misst und meldet, und was."],
  [/time|clock/, () => "The device's clock.", () => "Uhr des Geräts."],
];

// What one field of a group does, by the last part of its ID. A rule marked
// true reads the part as a number and holds for number fields only.
const PARTS = [
  [/alarm_(release|deactivation)_enable$/, "Also reports when the alarm ends.", "Meldet auch, wenn der Alarm wieder endet."],
  [/enable$/, "Switches this function on or off.", "Schaltet diese Funktion ein oder aus."],
  [/interval$/, "Time between two rounds.", "Zeitabstand zwischen zwei Durchläufen.", true],
  [/^(clear|reset|remove|delete|cancel|stop)|_(clear|reset|remove)$/, "Clears, resets or stops what the name says.", "Löscht, setzt zurück oder beendet, was der Name sagt."],
  [/^(query|fetch|read)_|^report_(status|attribute|heating_\w+)$/, "Asks the device for what the name says.", "Fordert vom Gerät an, was der Name sagt."],
  [/(^|_)in$/, "Applies to those coming in.", "Gilt für die Hereinkommenden."],
  [/(^|_)out$/, "Applies to those going out.", "Gilt für die Hinausgehenden."],
  [/condition/, "When it triggers: below, above, between or outside the thresholds. Disable switches it off.", "Wann es auslöst: unter, über, zwischen oder außerhalb der Schwellen. „Aus“ schaltet es ab."],
  [/lock_time/, "After an alarm, the device does not report the same alarm again for this long.", "Nach einem Alarm meldet das Gerät denselben Alarm so lange nicht erneut."],
  [/continue_time/, "The condition has to last this long before it triggers.", "So lange muss die Bedingung anhalten, bevor es auslöst."],
  [/calibration_value/, "Correction added to the measured value.", "Korrektur, die zum Messwert addiert wird."],
  [/mode$|method$|strategy$|algorithm$|pattern$|scene$|direction$|determination$/, "Chooses how it works.", "Wählt, wie es arbeitet."],
  [/(^|_)(max|h|upper)$/, "The upper value of the limit or threshold.", "Der obere Wert der Grenze oder Schwelle.", true],
  [/(^|_)(min|l|lower)$/, "The lower value of the limit or threshold.", "Der untere Wert der Grenze oder Schwelle.", true],
  [/threshold|mutation|sensitiv/, "The threshold from which it triggers.", "Die Schwelle, ab der es auslöst.", true],
  [/type$/, "Chooses the kind.", "Wählt die Art."],
  [/unit(_display)?$/, "The unit the device shows and reports the value in.", "Die Einheit, in der das Gerät den Wert zeigt und meldet."],
  [/timeout/, "After this time the device stops waiting.", "Nach dieser Zeit wartet das Gerät nicht länger.", true],
  [/(^|_)(monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekdays?|week_day)$/, "The day of the week it applies on.", "Der Wochentag, an dem es gilt."],
  [/(^|_)start(_|$)/, "The start of the period.", "Beginn des Zeitraums."],
  [/(^|_)end(_|$)/, "The end of the period.", "Ende des Zeitraums."],
  [/delay/, "The delay before the device reacts.", "Die Verzögerung, bevor das Gerät reagiert.", true],
  [/duration|(^|_)time$|(^|_)(hour|minute|minutes_of_time|seconds_of_time)$/, "A time, as the name says.", "Eine Zeitangabe, wie der Name sagt.", true],
  [/counts?$|times$|num$/, "How many times.", "Wie oft.", true],
  [/offset|compensation|gain/, "A correction to the normal value.", "Eine Korrektur gegenüber dem Normalwert.", true],
  [/period/, "After this time it repeats.", "Nach dieser Zeit wiederholt es sich.", true],
  [/height|distance|depth|range/, "A measure, in the unit shown.", "Ein Maß, in der gezeigten Einheit.", true],
  [/temperature|celsius|fahrenheit|setpoint|tolerance|deadband|dead_zone/, "A temperature, in the unit shown.", "Eine Temperatur, in der gezeigten Einheit.", true],
];

// The explanation of a field: its own text, else what its group is about
// plus what its part does, else what kind of value it is.
function explainField(id, field, lang) {
  // The user guide's text, if every model that has the field gives the same.
  const guides = ((field && field.models) || []).map((model) => ((MODEL_HELP[lang] || {})[model] || {})[id]);
  if (guides.length && guides.every((g) => g && g === guides[0])) return guides[0];
  const own = HELP[lang][id];
  if (own) return own;
  const last = id.split(".").pop();
  const col = lang === "de" ? 2 : 1;
  const quantity = (QUANTITIES.find(([pattern]) => pattern.test(id)) || [])[col] || "";
  const topic = TOPICS.find(([pattern]) => pattern.test(id));
  // A name of its own for the last part beats the general rule for it.
  const part = (id.includes(".") && HELP[lang][last]) || (PARTS.find(([pattern, , , numeric]) => pattern.test(last) && (!numeric || (field && field.type === "NUMBER"))) || [])[col];
  const texts = [topic ? topic[col](quantity) : "", part || ""].filter(Boolean);
  if (texts.length) return texts.join(" ");
  const name = (field && field.name) || pretty(id);
  if (lang === "de") {
    return `${field && field.type === "BOOL" ? `Schaltet „${name}“ ein oder aus.` : `Stellt „${name}“ ein.`} Milesight beschreibt dieses Feld nicht näher.`;
  }
  return `${field && field.type === "BOOL" ? `Switches "${name}" on or off.` : `Sets "${name}".`} Milesight does not describe this field any further.`;
}

// Texts from a model's user guide, where it describes a field; they beat the
// general ones. WT101: Milesight WT101 user guide V1.2, chapters 5 and 6.
const MODEL_HELP = {
  en: {
    wt101: {
      report_interval: "How often the thermostat reports battery level, temperature and motor status. Default 10 min, 1 to 1440 min.",
      display_ambient_temperature: "After the knob is turned, the display shows the room temperature for a moment.",
      "temperature_control.enable": "Lets the thermostat move the valve to regulate. Switched on, it calibrates the motor once.",
      "temperature_control.mode": "Auto: the thermostat regulates to the target temperature. Manual: it holds the valve opening you set.",
      target_temperature: "The room temperature to reach. Every 10 minutes the thermostat moves the valve if the room is further off than the tolerance.",
      temperature_tolerance: "How far the room may differ from the target temperature before the valve moves.",
      "target_temperature_range.min": "Lowest target temperature the knob can set, 5 to 15 °C. Default 10 °C.",
      "target_temperature_range.max": "Highest target temperature the knob can set, 16 to 35 °C. Default 28 °C.",
      "open_window_detection.enable": "When the temperature falls faster than the cooling rate, the thermostat stops regulating and reports an open window. It resumes after 10 minutes of rising temperature or at the target.",
      "open_window_detection.temperature_threshold": "The cooling rate, in °C per minute, from which a window counts as open.",
      "open_window_detection.time": "After this time the thermostat regulates again, even if the window still seems open.",
      window_detection_valve_strategy: "What the valve does on an open window: keep its position (default) or close.",
      restore_open_window_detection: "Ends the open window state now.",
      valve_opening: "Valve opening for manual mode: 0 % closed, 100 % fully open.",
      "freeze_protection_config.enable": "Below the threshold the thermostat opens the valve every 10 minutes until the room is back at the threshold.",
      "freeze_protection_config.temperature": "The temperature freeze protection holds.",
      "child_lock_config.enable": "Locks the knob; the valve then only changes by app or downlink.",
      time_zone: "The thermostat's time zone, for the heating schedule.",
      "dst_config.enable": "Switches daylight saving time on or off.",
      "dst_config.offset": "How far daylight saving time puts the clock forward, in minutes.",
      "dst_config.start_month": "Month in which daylight saving time starts.",
      "dst_config.start_week_num": "Week of that month: 1 for the first, 2 for the second.",
      "dst_config.start_week_day": "Weekday on which daylight saving time starts.",
      "dst_config.start_time": "Time of day it starts, in minutes after midnight: 120 is 2:00.",
      "dst_config.end_month": "Month in which daylight saving time ends.",
      "dst_config.end_week_num": "Week of that month: 1 for the first, 2 for the second.",
      "dst_config.end_week_day": "Weekday on which daylight saving time ends.",
      "dst_config.end_time": "Time of day it ends, in minutes after midnight: 120 is 2:00.",
      change_report_enable: "Sends a report at once when the target temperature or the valve opening changes.",
      offline_control_mode: "What the thermostat does when it loses the network server: keep the valve, regulate by its own sensor, or stop regulating.",
      valve_calibration: "Measures the motor stroke again. Needed after mounting on another valve.",
      "effective_stroke.enable": "Limits how far the valve opens, for hydraulic balancing.",
      "effective_stroke.rate": "Largest valve opening in percent of the full stroke.",
      "temperature_calibration_settings.enable": "Switches the temperature correction on or off.",
      "temperature_calibration_settings.calibration_value": "Added to the measured temperature; the thermostat reports and regulates by the result.",
      "heating_date.enable": "Restricts regulation to a heating period; outside it the thermostat does not regulate.",
      "heating_date.start_month": "Month the heating period starts.",
      "heating_date.start_day": "Day the heating period starts.",
      "heating_date.end_month": "Month the heating period ends.",
      "heating_date.end_day": "Day the heating period ends.",
      "heating_date.report_interval": "Report interval outside the heating period.",
      report_heating_date: "Asks for the heating period that is set.",
      report_heating_schedule: "Asks for the 16 heating events.",
      "outside_temperature_control.enable": "Regulates by a temperature sent by downlink instead of the built-in sensor.",
      "outside_temperature_control.timeout": "Without a new outside temperature for this long, the thermostat stops regulating. 3 to 60 min.",
      outside_temperature: "The temperature measured elsewhere, sent to the thermostat.",
      time_sync_enable: "The thermostat asks the network server for the time. Needs LoRaWAN 1.0.3.",
      reboot: "Restarts the thermostat.",
      report_status: "Asks for a status report.",
      sync_time: "Sets the thermostat's clock.",
    },
  },
  de: {
    wt101: {
      report_interval: "Wie oft das Thermostat Batteriestand, Temperatur und Motorstatus meldet. Werkseinstellung 10 min, 1 bis 1440 min.",
      display_ambient_temperature: "Nach einem Dreh am Knopf zeigt die Anzeige kurz die Raumtemperatur.",
      "temperature_control.enable": "Lässt das Thermostat das Ventil zum Regeln bewegen. Beim Einschalten kalibriert es den Motor einmal.",
      "temperature_control.mode": "Auto: das Thermostat regelt auf die Solltemperatur. Manuell: es hält die eingestellte Ventilöffnung.",
      target_temperature: "Die Raumtemperatur, die erreicht werden soll. Alle 10 Minuten verstellt das Thermostat das Ventil, wenn der Raum weiter als die Toleranz abweicht.",
      temperature_tolerance: "Wie weit der Raum von der Solltemperatur abweichen darf, bevor das Ventil verstellt wird.",
      "target_temperature_range.min": "Niedrigste Solltemperatur, die sich am Knopf einstellen lässt, 5 bis 15 °C. Werkseinstellung 10 °C.",
      "target_temperature_range.max": "Höchste Solltemperatur, die sich am Knopf einstellen lässt, 16 bis 35 °C. Werkseinstellung 28 °C.",
      "open_window_detection.enable": "Fällt die Temperatur schneller als die Abkühlrate, hört das Thermostat auf zu regeln und meldet ein offenes Fenster. Nach 10 Minuten steigender Temperatur oder am Sollwert regelt es weiter.",
      "open_window_detection.temperature_threshold": "Die Abkühlrate in °C pro Minute, ab der ein Fenster als offen gilt.",
      "open_window_detection.time": "Nach dieser Zeit regelt das Thermostat wieder, auch wenn das Fenster noch offen scheint.",
      window_detection_valve_strategy: "Was das Ventil bei offenem Fenster tut: Stellung halten (Werkseinstellung) oder schließen.",
      restore_open_window_detection: "Beendet den Zustand „Fenster offen“ sofort.",
      valve_opening: "Ventilöffnung im manuellen Modus: 0 % zu, 100 % ganz offen.",
      "freeze_protection_config.enable": "Unter der Schwelle öffnet das Thermostat alle 10 Minuten das Ventil, bis der Raum wieder bei der Schwelle ist.",
      "freeze_protection_config.temperature": "Die Temperatur, die der Frostschutz hält.",
      "child_lock_config.enable": "Sperrt den Drehknopf; das Ventil lässt sich dann nur per App oder Downlink verstellen.",
      time_zone: "Die Zeitzone des Thermostats, für den Heizplan.",
      "dst_config.enable": "Schaltet die Sommerzeit ein oder aus.",
      "dst_config.offset": "Um wie viele Minuten die Sommerzeit die Uhr vorstellt.",
      "dst_config.start_month": "Monat, in dem die Sommerzeit beginnt.",
      "dst_config.start_week_num": "Woche des Monats: 1 für die erste, 2 für die zweite.",
      "dst_config.start_week_day": "Wochentag, an dem die Sommerzeit beginnt.",
      "dst_config.start_time": "Uhrzeit des Beginns in Minuten nach Mitternacht: 120 ist 2:00.",
      "dst_config.end_month": "Monat, in dem die Sommerzeit endet.",
      "dst_config.end_week_num": "Woche des Monats: 1 für die erste, 2 für die zweite.",
      "dst_config.end_week_day": "Wochentag, an dem die Sommerzeit endet.",
      "dst_config.end_time": "Uhrzeit des Endes in Minuten nach Mitternacht: 120 ist 2:00.",
      change_report_enable: "Sendet sofort eine Meldung, wenn sich Solltemperatur oder Ventilöffnung ändern.",
      offline_control_mode: "Was das Thermostat tut, wenn es den Network Server verliert: Ventil halten, mit eigenem Sensor regeln oder nicht mehr regeln.",
      valve_calibration: "Vermisst den Motorhub neu. Nötig nach der Montage auf einem anderen Ventil.",
      "effective_stroke.enable": "Begrenzt, wie weit das Ventil öffnet, für den hydraulischen Abgleich.",
      "effective_stroke.rate": "Größte Ventilöffnung in Prozent des vollen Hubs.",
      "temperature_calibration_settings.enable": "Schaltet die Temperaturkorrektur ein oder aus.",
      "temperature_calibration_settings.calibration_value": "Wird zur gemessenen Temperatur addiert; das Thermostat meldet und regelt nach dem Ergebnis.",
      "heating_date.enable": "Beschränkt das Regeln auf eine Heizperiode; außerhalb regelt das Thermostat nicht.",
      "heating_date.start_month": "Monat, in dem die Heizperiode beginnt.",
      "heating_date.start_day": "Tag, an dem die Heizperiode beginnt.",
      "heating_date.end_month": "Monat, in dem die Heizperiode endet.",
      "heating_date.end_day": "Tag, an dem die Heizperiode endet.",
      "heating_date.report_interval": "Sendeintervall außerhalb der Heizperiode.",
      report_heating_date: "Fragt die eingestellte Heizperiode ab.",
      report_heating_schedule: "Fragt die 16 Heizereignisse ab.",
      "outside_temperature_control.enable": "Regelt nach einer per Downlink gesendeten Temperatur statt nach dem eingebauten Sensor.",
      "outside_temperature_control.timeout": "Kommt so lange keine neue Außentemperatur, hört das Thermostat auf zu regeln. 3 bis 60 min.",
      outside_temperature: "Die an anderer Stelle gemessene Temperatur, die das Thermostat bekommt.",
      time_sync_enable: "Das Thermostat fragt den Network Server nach der Uhrzeit. Braucht LoRaWAN 1.0.3.",
      reboot: "Startet das Thermostat neu.",
      report_status: "Fordert einen Statusbericht an.",
      sync_time: "Stellt die Uhr des Thermostats.",
    },
  },
};

// Catalog value names in German; the catalog names them in English only.
const VALUE_DE = {
  january: "Januar", february: "Februar", march: "März", april: "April", may: "Mai", june: "Juni",
  july: "Juli", august: "August", september: "September", october: "Oktober", november: "November", december: "Dezember",
  monday: "Montag", tuesday: "Dienstag", wednesday: "Mittwoch", thursday: "Donnerstag", friday: "Freitag",
  saturday: "Samstag", sunday: "Sonntag",
  disable: "Aus", enable: "An", off: "Aus", on: "An", no: "Nein", yes: "Ja",
  keep: "Beibehalten", close: "Schließen", open: "Öffnen", auto: "Automatisch", manual: "Manuell",
  "embedded temperature control": "Eigener Sensor", rate: "Rate", pid: "PID",
  period: "Periodisch", immediately: "Sofort", below: "Unter", above: "Über", between: "Zwischen", outside: "Außerhalb",
  horizontal: "Waagerecht", vertical: "Senkrecht", normal: "Normal", high: "Hoch", low: "Niedrig", medium: "Mittel",
};

// Time units that may be converted into each other, in seconds.
const TIME = { s: 1, min: 60, h: 3600 };
const BARS = { excellent: 4, good: 3, fair: 2, poor: 1 };
// What a command's state says, as a colour.
const TONE = { confirmed: "ok", acknowledged: "ok", pending: "warn", retrying: "warn", transmitted: "warn", failed: "bad", differs: "bad", sent: "muted" };
// Settings everyone looks for come first, in this order.
const FIRST = ["report_interval", "reporting_interval", "collection_interval"];
// Model name -> icon, first match wins; the families Milesight builds.
const ICONS = [
  [/^WT/, "mdi:radiator"],
  [/^WS101/, "mdi:gesture-tap-button"],
  [/^WS15|^WS13/, "mdi:gesture-tap-button"],
  [/^WS20/, "mdi:motion-sensor"],
  [/^WS301|MCS/, "mdi:door"],
  [/^WS303|SLD|ZLD|MLD|LEAK/, "mdi:water-alert"],
  [/^WS52/, "mdi:power-socket-eu"],
  [/^WS5/, "mdi:light-switch"],
  [/^VS12|^VS13|^VS32|^VS35/, "mdi:account-group"],
  [/^VS/, "mdi:seat"],
  [/^AM/, "mdi:air-filter"],
  [/^EM300-DI|^UC/, "mdi:counter"],
  [/^EM300-CL/, "mdi:cup-water"],
  [/^EM31|^EM400|UDL/, "mdi:arrow-expand-vertical"],
  [/CO2/, "mdi:molecule-co2"],
  [/^EM500/, "mdi:gauge"],
  [/^EM|^TS/, "mdi:thermometer"],
  [/^GS/, "mdi:smoke-detector"],
  [/^CT/, "mdi:current-ac"],
  [/^DS/, "mdi:tablet"],
];

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const icon = (name, cls = "") => `<ha-icon class="${cls}" icon="${esc(name)}"></ha-icon>`;
const pretty = (text) => { const s = String(text).replace(/_/g, " ").trim(); return s.charAt(0).toUpperCase() + s.slice(1); };
const num = (v) => (typeof v === "number" ? String(Math.round(v * 100) / 100) : String(v));
const blank = (v) => v === undefined || v === null || v === "";

function modelIcon(name) {
  const upper = String(name || "").toUpperCase();
  const hit = ICONS.find(([pattern]) => pattern.test(upper));
  return hit ? hit[1] : "mdi:access-point";
}

function batteryIcon(level) {
  const step = Math.max(0, Math.min(100, Math.round(level / 10) * 10));
  if (step === 0) return "mdi:battery-outline";
  return step === 100 ? "mdi:battery" : `mdi:battery-${step}`;
}

// "SITE_B_X_EG_R12_..." names carry floor and room as parts 4 and 5.
function place(name) {
  const p = String(name || "").split("_");
  return p.length >= 6 ? { floor: p[3], room: p[4] } : { floor: "", room: "" };
}

function same(a, b) {
  return a.type === b.type && (a.unit === b.unit || (TIME[a.unit] && TIME[b.unit]));
}

// "Humidity Alarm Config (Lock Time)" -> ["Humidity Alarm Config", "Lock Time"].
function splitName(name) {
  const hit = /^(.+?)\s*\((.+)\)$/.exec(String(name || ""));
  return hit ? [hit[1], hit[2]] : null;
}

const STYLE = `
  :host {
    --ok: var(--success-color, #43a047); --warn: var(--warning-color, #ffa600);
    --bad: var(--error-color, #db4437); --accent: var(--primary-color, #03a9f4);
    --muted: var(--secondary-text-color); --line: var(--divider-color);
    --card: var(--card-background-color); --soft: var(--secondary-background-color);
    display: block; min-height: 100vh; box-sizing: border-box;
    background: var(--primary-background-color); color: var(--primary-text-color);
    font-family: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, Roboto, sans-serif));
    font-size: 14px; line-height: 1.4;
  }
  * { box-sizing: border-box; }
  .page { padding: 16px 16px 48px; max-width: 2200px; margin: 0 auto; }
  header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  h1 { font-size: 24px; font-weight: 400; margin: 0; }
  h1 a { color: inherit; text-decoration: none; }
  h1 a:hover { text-decoration: underline; }
  a.gw { color: inherit; text-decoration: none; }
  a.gw:hover { color: var(--accent); text-decoration: underline; }
  .tally { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-weight: 400; }
  button.pill { border: 1px solid transparent; min-height: 0; padding: 2px 10px; cursor: pointer; }
  button.pill:hover:not(:disabled) { border-color: currentColor; background: color-mix(in srgb, currentColor 16%, transparent); }
  button.pill.on { border-color: currentColor; background: color-mix(in srgb, currentColor 32%, transparent); }
  header .counts { color: var(--muted); margin-left: auto; }
  h2 { font-size: 16px; font-weight: 500; margin: 0; display: flex; align-items: center; gap: 8px; }
  h2 small, .sub { color: var(--muted); font-weight: 400; font-size: 13px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: var(--ha-card-border-radius, 12px); margin-bottom: 16px; }
  .head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 12px 16px; }
  .head .grow { flex: 1; }
  .body { padding: 0 16px 16px; }
  .empty { color: var(--muted); display: flex; align-items: center; gap: 10px; padding: 8px 0; }
  .error { color: var(--bad); }
  ha-icon { --mdc-icon-size: 20px; }

  input, select, textarea, button { font: inherit; color: inherit; }
  input[type=text], input[type=search], input[type=number], select, textarea {
    background: var(--soft); border: 1px solid transparent; border-radius: 8px; padding: 6px 10px; min-height: 34px; outline: none;
  }
  input:focus, select:focus, textarea:focus { border-color: var(--accent); }
  textarea { width: 100%; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 13px; }
  .search { position: relative; }
  .search ha-icon { position: absolute; left: 8px; top: 7px; color: var(--muted); }
  .search input { padding-left: 34px; width: 240px; }
  button { cursor: pointer; border-radius: 8px; min-height: 34px; padding: 6px 14px; border: 1px solid var(--line); background: none; }
  button:hover:not(:disabled) { background: var(--soft); }
  button.primary { background: var(--accent); border-color: var(--accent); color: var(--text-primary-color, #fff); font-weight: 500; }
  button.primary:hover:not(:disabled) { background: var(--accent); filter: brightness(1.08); }
  button:disabled { opacity: .45; cursor: default; }
  button.link { border: none; color: var(--accent); padding: 0; min-height: 0; text-decoration: underline; }
  button.link:hover:not(:disabled) { background: none; }
  .count { color: var(--muted); }
  .count.some { color: var(--accent); font-weight: 500; }

  .scroll { max-height: 52vh; overflow: auto; border-top: 1px solid var(--line); }
  table { border-collapse: separate; border-spacing: 0; width: 100%; }
  th { position: sticky; top: 0; z-index: 1; background: var(--card); text-align: left; font-weight: 500; font-size: 12px;
       color: var(--muted); padding: 8px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th.sort { cursor: pointer; user-select: none; }
  th.sort:hover, th.sorted { color: var(--primary-text-color); }
  th ha-icon { --mdc-icon-size: 14px; vertical-align: -2px; }
  td { padding: 6px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tbody tr.pick { cursor: pointer; }
  tbody tr.pick:hover td { background: var(--soft); }
  tbody tr.sel td { background: color-mix(in srgb, var(--accent) 12%, transparent); }
  tbody tr.sel:hover td { background: color-mix(in srgb, var(--accent) 18%, transparent); }
  tr.off .name, tr.off .avatar { opacity: .55; }
  td.check, th.check { width: 36px; padding-right: 0; }
  input[type=checkbox] { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; vertical-align: middle; }
  .dev { display: flex; align-items: center; gap: 12px; }
  .avatar { width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; flex: none; text-decoration: none;
            background: color-mix(in srgb, var(--accent) 14%, transparent); color: var(--accent); }
  a.avatar:hover { background: var(--accent); color: var(--text-primary-color, #fff); }
  .name { font-weight: 500; }
  .mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px; color: var(--muted); }
  .chip { display: inline-block; padding: 2px 8px; border-radius: 6px; background: var(--soft); font-size: 12px; white-space: nowrap; }
  .ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); } .muted { color: var(--muted); }
  .dot { display: inline-flex; align-items: center; gap: 6px; font-weight: 500; }
  .dot::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
  .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 500;
          background: color-mix(in srgb, currentColor 16%, transparent); }
  .pill.muted { background: var(--soft); }
  .sig { display: flex; align-items: center; gap: 10px; }
  .bars { display: inline-flex; align-items: flex-end; gap: 2px; height: 16px; }
  .bars i { width: 4px; border-radius: 1px; background: currentColor; opacity: .22; }
  .bars i.f { opacity: 1; }
  .bars i:nth-child(1) { height: 4px; } .bars i:nth-child(2) { height: 8px; }
  .bars i:nth-child(3) { height: 12px; } .bars i:nth-child(4) { height: 16px; }
  .batt { display: inline-flex; align-items: center; gap: 4px; }

  .tags { display: flex; flex-wrap: wrap; gap: 6px; }
  .group { margin-top: 20px; }
  .group > h3 { font-size: 13px; font-weight: 500; color: var(--muted); margin: 0 0 6px; padding-bottom: 4px; border-bottom: 1px solid var(--line); }
  .sets { display: grid; grid-template-columns: repeat(auto-fill, minmax(400px, 1fr)); gap: 4px 24px; align-items: start; }
  .set { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding: 8px 10px; border-radius: 8px; border-left: 3px solid transparent; min-height: 52px; }
  .set:hover { background: var(--soft); }
  .set.changed { border-left-color: var(--accent); background: color-mix(in srgb, var(--accent) 9%, transparent); }
  .set .label { flex: 1 1 150px; min-width: 0; }
  .set .label b { font-weight: 500; display: block; overflow-wrap: anywhere; }
  .set .label small { color: var(--muted); display: block; overflow-wrap: anywhere; }
  .set .only { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }
  .set .tool { border: none; min-height: 0; padding: 2px; color: var(--muted); border-radius: 50%; display: grid; place-items: center; }
  .set .tool:hover { color: var(--primary-text-color); }
  .set .tool.open { color: var(--accent); }
  .set .undo { visibility: hidden; }
  .q { position: relative; display: inline-flex; }
  .q .tip { display: none; position: absolute; right: 0; top: calc(100% + 6px); z-index: 5; width: 320px;
            padding: 8px 10px; border-radius: 8px; font-size: 13px; line-height: 1.4; white-space: normal;
            background: var(--card); color: var(--primary-text-color); border: 1px solid var(--line);
            box-shadow: 0 4px 14px rgba(0, 0, 0, .35); pointer-events: none; }
  .q:hover .tip { display: block; }
  .set.changed .undo { visibility: visible; }
  .set .help { flex-basis: 100%; background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; font-size: 13px; }
  .set .help p { margin: 0 0 8px; }
  .set .help dl { display: grid; grid-template-columns: auto 1fr; gap: 2px 12px; margin: 0; }
  .set .help dt { color: var(--muted); } .set .help dd { margin: 0; overflow-wrap: anywhere; }
  .numbox { display: flex; align-items: center; gap: 6px; }
  .numbox input { width: 110px; text-align: right; }
  .numbox .unit { color: var(--muted); min-width: 28px; }
  .seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .seg button { border: none; border-radius: 0; min-height: 32px; padding: 4px 12px; border-left: 1px solid var(--line); position: relative; white-space: nowrap; }
  .seg button:first-child { border-left: none; }
  .seg button.on { background: var(--accent); color: var(--text-primary-color, #fff); }
  .seg button.cur:not(.on)::after { content: ""; position: absolute; left: 50%; bottom: 3px; width: 14px; height: 2px;
       margin-left: -7px; border-radius: 1px; background: var(--accent); }
  .cmds { display: flex; flex-wrap: wrap; gap: 8px; }
  .cmds button { border-radius: 999px; display: inline-flex; align-items: center; gap: 6px; }
  .cmds button.on { background: var(--accent); border-color: var(--accent); color: var(--text-primary-color, #fff); }
  .actions { position: sticky; bottom: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin: 16px -16px -16px;
             padding: 12px 16px; background: var(--card); border-top: 1px solid var(--line);
             border-radius: 0 0 var(--ha-card-border-radius, 12px) var(--ha-card-border-radius, 12px); }
  .actions .grow { flex: 1; color: var(--muted); }
  .actions .grow.some { color: var(--primary-text-color); font-weight: 500; }
  .outcome { margin-top: 12px; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .outcome td { padding: 6px 12px; }
  .kv { display: inline-block; margin-right: 10px; }
  .kv span { color: var(--muted); }
  details > summary { list-style: none; cursor: pointer; }
  details > summary::-webkit-details-marker { display: none; }
  details .chev { margin-left: auto; color: var(--muted); transition: transform .15s; }
  details[open] .chev { transform: rotate(180deg); }
  .form { display: flex; flex-wrap: wrap; gap: 12px 16px; margin-bottom: 12px; }
  .form label { display: flex; flex-direction: column; gap: 4px; color: var(--muted); font-size: 12px; }
  .form label select { color: var(--primary-text-color); font-size: 14px; }
  @media (max-width: 700px) {
    .sets { grid-template-columns: 1fr; }
    .search input { width: 100%; }
  }
`;

class MilesightDevicesPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.data = { devices: [], models: {}, gateways: [] };
    this.records = [];
    this.selected = new Set();
    this.filter = { q: "", gateway: "", type: "", floor: "", room: "" };
    this.sort = { key: "name", dir: 1 };
    this.form = {};
    this.helpOpen = new Set();
    this.find = "";
    this.preview = null;
    this.results = null;
    this.busy = false;
    this.imp = { entry_id: "", choices: null, application: "", profile: "", codec: "auto", text: "", results: null, error: null };
    this.logFilter = null;
    this.logSort = { key: "time", dir: -1 };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      const lang = (hass.language || "en").startsWith("de") ? "de" : "en";
      this.t = TEXT[lang];
      this.lang = lang;
      this.shadowRoot.innerHTML = `<style>${STYLE}</style><div class="page">
        <header><span id="menu"></span><h1><a class="nav" href="/config/integrations/integration/milesight_gateway" title="${esc(this.t.toIntegration)}">${esc(this.t.title)}</a></h1><span class="counts" id="counts"></span></header>
        <div id="error"></div>
        <section class="card"><div class="head" id="toolbar"></div><div class="scroll" id="rows"></div></section>
        <section class="card" id="settings"></section>
        <details class="card" id="import"></details>
        <section class="card" id="log"></section>
      </div>`;
      this.menu();
      this.shadowRoot.addEventListener("click", (e) => {
        const link = e.composedPath().find((el) => el.tagName === "A" && el.classList && el.classList.contains("nav"));
        if (!link) return;
        // Home Assistant's own navigation: no reload of the whole frontend.
        e.preventDefault();
        e.stopPropagation();
        history.pushState(null, "", link.getAttribute("href"));
        window.dispatchEvent(new CustomEvent("location-changed"));
      });
      this.start();
    }
  }

  set narrow(value) {
    this._narrow = value;
    this.menu();
  }

  // The sidebar's button, for a window too narrow to show the sidebar.
  menu() {
    const slot = this.$("menu");
    if (!slot) return;
    slot.innerHTML = "";
    if (!this._narrow) return;
    const button = document.createElement("ha-menu-button");
    button.hass = this._hass;
    button.narrow = true;
    slot.append(button);
  }

  connectedCallback() {
    if (this._hass && !this._timer) this.start();
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
  }

  start() {
    this.load();
    this._timer = setInterval(() => this.load(), 10000);
  }

  $(id) {
    return this.shadowRoot.getElementById(id);
  }

  // Whether the user is typing or choosing inside a part of the page.
  busyIn(id) {
    const active = this.shadowRoot.activeElement;
    const part = this.$(id);
    return Boolean(active && part && part.contains(active) && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName));
  }

  async load() {
    try {
      const [data, log] = await Promise.all([
        this._hass.callWS({ type: "milesight_gateway/devices" }),
        this._hass.callWS({ type: "milesight_gateway/log" }),
      ]);
      this.data = data;
      this.records = log.records;
      this.error = null;
    } catch (err) {
      this.error = err.message || String(err);
    }
    // Devices the gateway dropped leave the selection.
    const keys = new Set(this.data.devices.map((d) => this.key(d)));
    for (const key of [...this.selected]) if (!keys.has(key)) this.selected.delete(key);
    this.$("error").innerHTML = this.error ? `<div class="card"><div class="head error">${icon("mdi:alert-circle-outline")} ${esc(this.error)}</div></div>` : "";
    const online = this.data.devices.filter((d) => d.active).length;
    this.$("counts").textContent = this.t.counts(this.data.devices.length, online, this.data.devices.length - online);
    if (!this.busyIn("toolbar")) this.renderToolbar();
    this.renderRows();
    if (!this.busyIn("settings")) this.renderSettings();
    if (!this._importDrawn || (!this.busyIn("import") && this._importGateways !== (this.data.gateways || []).length)) this.renderImport();
    this.renderLog();
  }

  key(d) {
    return `${d.entry_id}|${d.dev_eui}`;
  }

  model(d) {
    return this.data.models[d.model_id] || {};
  }

  // What a column sorts by; null sorts last, whichever way.
  sortValue(d, key) {
    switch (key) {
      case "model": return this.model(d).name || d.model_id;
      case "gateway": return d.gateway;
      case "status": return d.active ? 1 : 0;
      case "seen": return d.last_seen ? Date.parse(d.last_seen) : null;
      case "battery": return d.battery;
      case "signal": return d.margin ?? d.rssi;
      case "loss": return d.packet_loss;
      case "command": return d.command.state;
      case "interval": return (this.interval(d) || {}).sec ?? null;
      default: return d.name;
    }
  }

  shown() {
    const f = this.filter;
    const q = f.q.toLowerCase();
    const { key, dir } = this.sort;
    return this.data.devices.filter((d) => {
      const p = place(d.name);
      return (!q || `${d.name} ${this.model(d).name} ${d.dev_eui}`.toLowerCase().includes(q))
        && (!f.gateway || d.gateway === f.gateway)
        && (!f.type || this.model(d).name === f.type)
        && (!f.floor || p.floor === f.floor)
        && (!f.room || p.room === f.room);
    }).sort((a, b) => {
      const x = this.sortValue(a, key), y = this.sortValue(b, key);
      if (blank(x) || blank(y)) return blank(x) === blank(y) ? a.name.localeCompare(b.name) : blank(x) ? 1 : -1;
      const order = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
      return order * dir || a.name.localeCompare(b.name);
    });
  }

  chosen() {
    return this.data.devices.filter((d) => this.selected.has(this.key(d)) && d.sends);
  }

  // The settings (or commands) of the chosen devices' models. One that every
  // model has is shared; one only some have says which ("only"). Time units
  // are shown in the coarsest one among the models.
  collect(kind) {
    const lists = [...new Set(this.chosen().map((d) => d.model_id))].map((id) => ({
      id,
      name: (this.data.models[id] || {}).name || id,
      fields: (this.data.models[id] || {})[kind] || {},
    }));
    const out = {};
    for (const list of lists) {
      for (const [id, f] of Object.entries(list.fields)) {
        if (id in out) continue;
        const holders = lists.filter((l) => l.fields[id]);
        const all = holders.map((l) => l.fields[id]);
        if (all.some((x) => !same(x, f))) { out[id] = null; continue; }
        const coarse = all.reduce((a, x) => ((TIME[x.unit] || 0) > (TIME[a.unit] || 0) ? x : a), f);
        // A range or a default in another unit says nothing about the shown one.
        const mixed = all.some((x) => x.unit !== coarse.unit);
        out[id] = {
          ...f,
          ...(mixed ? { unit: coarse.unit, min: undefined, max: undefined, default: undefined } : {}),
          only: holders.length < lists.length ? holders.map((l) => l.name) : null,
          models: holders.map((l) => l.id),
        };
      }
    }
    return Object.fromEntries(Object.entries(out).filter(([, f]) => f));
  }

  fields() {
    const commands = Object.fromEntries(Object.entries(this.collect("commands")).filter(([, f]) => f.type === "BOOL"));
    return { settings: this.collect("settings"), commands };
  }

  // One device's changes from the form, in its own unit; only what its model has.
  changes(device, shown) {
    const fields = { ...this.model(device).settings, ...this.model(device).commands };
    const out = {};
    for (const [id, raw] of Object.entries(this.form)) {
      const f = fields[id];
      if (blank(raw) || !f || !shown[id]) continue;
      let v = Number(raw);
      if (!Number.isFinite(v)) continue;
      if (f.type === "NUMBER" && TIME[shown[id].unit] && TIME[f.unit]) {
        v = (v * TIME[shown[id].unit]) / TIME[f.unit];
      }
      if (f.value_type !== "FLOAT") v = Math.round(v);
      out[id] = v;
    }
    return out;
  }

  items() {
    const { settings, commands } = this.fields();
    const shown = { ...settings, ...commands };
    return this.chosen().map((d) => ({ entry_id: d.entry_id, dev_eui: d.dev_eui, changes: this.changes(d, shown) }))
      .filter((i) => Object.keys(i.changes).length);
  }

  // What the chosen devices that have a setting are known to have for it, in
  // the shown unit: the one value they share, or that they differ.
  known(id, shown) {
    const values = this.chosen().filter((d) => (this.model(d).settings || {})[id]).map((d) => {
      const kept = d.settings[id];
      if (!kept) return null;
      const own = this.model(d).settings[id];
      let v = kept.v;
      if (typeof v === "number" && TIME[own.unit] && TIME[shown.unit]) v = (v * TIME[own.unit]) / TIME[shown.unit];
      return { v, src: kept.src };
    });
    if (!values.length || values.some((v) => !v)) return null;
    const first = num(values[0].v);
    if (values.some((v) => num(v.v) !== first)) return { mixed: true };
    const src = values.every((v) => v.src === values[0].src) ? values[0].src : null;
    return { raw: first, src };
  }

  optionNames(f) {
    const t = this.t;
    const words = { disable: t.off, off: t.off, no: t.off, enable: t.on, on: t.on, yes: t.on };
    const values = f.values || (f.type === "BOOL" ? { 0: "off", 1: "on" } : {});
    const local = this.lang === "de" ? VALUE_DE : {};
    return Object.entries(values).map(([raw, name]) => {
      const word = String(name).toLowerCase();
      return [raw, (f.type === "BOOL" && words[word]) || local[word] || pretty(name)];
    });
  }

  valueText(f, raw) {
    if (f.type === "NUMBER") return `${raw}${f.unit ? ` ${f.unit}` : ""}`;
    const hit = this.optionNames(f).find(([value]) => value === String(raw));
    return hit ? hit[1] : String(raw);
  }

  // ---- list ---------------------------------------------------------------

  renderToolbar() {
    const t = this.t;
    const devices = this.data.devices;
    const places = devices.map((d) => place(d.name));
    const options = (values, all, current) => `<option value="">${esc(all)}</option>${[...new Set(values)].filter(Boolean).sort()
      .map((v) => `<option ${v === current ? "selected" : ""}>${esc(v)}</option>`).join("")}`;
    this.$("toolbar").innerHTML = `
      <span class="search">${icon("mdi:magnify")}<input id="q" type="search" placeholder="${esc(t.search)}" value="${esc(this.filter.q)}"></span>
      <select id="gateway">${options(devices.map((d) => d.gateway), t.allGateways, this.filter.gateway)}</select>
      <select id="type">${options(devices.map((d) => this.model(d).name), t.allTypes, this.filter.type)}</select>
      ${places.some((p) => p.floor) ? `<select id="floor">${options(places.map((p) => p.floor), t.allFloors, this.filter.floor)}</select>
      <select id="room">${options(places.map((p) => p.room), t.allRooms, this.filter.room)}</select>` : ""}
      <span class="grow"></span>
      <span class="count" id="count"></span>
      <button id="none">${esc(t.clear)}</button>`;
    this.$("q").oninput = (e) => { this.filter.q = e.target.value; this.renderRows(); };
    for (const id of ["gateway", "type", "floor", "room"]) {
      const el = this.$(id);
      if (el) el.onchange = (e) => { this.filter[id] = e.target.value; this.renderRows(); };
    }
    this.$("none").onclick = () => { this.selected.clear(); this.selectionChanged(); };
    this.updateCount();
  }

  updateCount() {
    const el = this.$("count");
    if (!el) return;
    el.textContent = this.t.selected(this.selected.size);
    el.classList.toggle("some", this.selected.size > 0);
    this.$("none").disabled = !this.selected.size;
  }

  ago(iso) {
    if (!iso) return this.t.never;
    const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    const text = s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 129600 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
    return this.t.ago(text);
  }

  pill(state) {
    if (!state) return `<span class="muted">–</span>`;
    return `<span class="pill ${TONE[state] || "muted"}">${esc(this.t.states[state] || state)}</span>`;
  }

  signal(d) {
    if (d.rssi == null) return `<span class="muted">–</span>`;
    // Without the spreading factor there is no quality; the RSSI fills the bars.
    const filled = d.quality ? BARS[d.quality] : d.rssi >= -85 ? 4 : d.rssi >= -100 ? 3 : d.rssi >= -110 ? 2 : 1;
    const tone = d.quality ? { excellent: "ok", good: "ok", fair: "warn", poor: "bad" }[d.quality] : "muted";
    const bars = `<span class="bars ${tone}">${[1, 2, 3, 4].map((i) => `<i class="${i <= filled ? "f" : ""}"></i>`).join("")}</span>`;
    const label = d.quality ? `<span class="${tone}">${esc(this.t.quality[d.quality])}</span>` : "";
    return `<span class="sig">${bars}<span>${label}<div class="sub">${esc(num(d.rssi))} dBm${d.snr != null ? ` · ${esc(num(d.snr))} dB` : ""}</div></span></span>`;
  }

  battery(level) {
    if (level == null) return `<span class="muted">–</span>`;
    const tone = level < 15 ? "bad" : level < 30 ? "warn" : "ok";
    return `<span class="batt">${icon(batteryIcon(level), tone)}${esc(num(level))} %</span>`;
  }

  loss(value) {
    if (value == null) return `<span class="muted">–</span>`;
    return `<span class="${value > 20 ? "bad" : value > 5 ? "warn" : "ok"}">${esc(num(value))} %</span>`;
  }

  gatewayCell(d) {
    const t = this.t;
    const name = d.gateway_device_id
      ? `<a class="gw nav" href="/config/devices/device/${esc(d.gateway_device_id)}" title="${esc(t.openGateway)}">${esc(d.gateway)}</a>`
      : esc(d.gateway);
    const heard = d.heard_by || [];
    let via = "";
    if (heard.length) {
      const best = heard[0];
      const label = best.name || best.eui;
      const all = heard.map((h) => `${h.name || h.eui}: ${h.rssi ?? "–"} dBm`).join("\n");
      const shown = best.device_id
        ? `<a class="gw nav" href="/config/devices/device/${esc(best.device_id)}">${esc(label)}</a>`
        : esc(label);
      via = ` · <span title="${esc(all)}">${esc(t.via)} ${shown}${heard.length > 1 ? ` +${heard.length - 1}` : ""}</span>`;
    }
    return `${name}<div class="sub">${esc(t.paths[d.path] || d.path)}${via}</div>`;
  }

  // How often the device reports, in seconds: the value it is set to, where
  // known, else the gap between its last two uplinks.
  interval(d) {
    const settings = this.model(d).settings || {};
    for (const key of FIRST.slice(0, 2)) {
      const kept = d.settings[key];
      const unit = TIME[(settings[key] || {}).unit];
      if (kept && typeof kept.v === "number" && unit && kept.src !== "sent") return { sec: kept.v * unit, measured: false };
    }
    return d.interval ? { sec: d.interval, measured: true } : null;
  }

  duration(sec) {
    const s = Math.abs(sec);
    return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`;
  }

  intervalCell(d) {
    const t = this.t;
    const every = this.interval(d);
    if (!every) return `<span class="muted">–</span>`;
    let sub = every.measured ? t.measured : "";
    // A command waits for the device's next uplink; tell when that is due.
    // "acknowledged" may be final already; the page cannot tell, so no countdown.
    if (["pending", "retrying", "transmitted"].includes(d.command.state) && d.last_seen) {
      const gap = d.interval || every.sec;
      const left = (Date.parse(d.last_seen) + gap * 1000 - Date.now()) / 1000;
      sub = left > 0 ? t.nextIn(this.duration(left)) : t.overdue;
    }
    return `${esc(this.duration(every.sec))}${sub ? `<div class="sub">${esc(sub)}</div>` : ""}`;
  }

  // The devices the header's checkbox stands for: shown, online, taking settings.
  pickable() {
    return this.shown().filter((d) => d.sends && d.active);
  }

  renderRows() {
    const t = this.t;
    const box = this.$("rows");
    const top = box.scrollTop;
    const column = (key, label) => {
      const on = this.sort.key === key;
      return `<th class="sort ${on ? "sorted" : ""}" data-sort="${key}">${esc(label)} ${on ? icon(this.sort.dir > 0 ? "mdi:arrow-up" : "mdi:arrow-down") : ""}</th>`;
    };
    box.innerHTML = `<table>
      <thead><tr><th class="check"><input type="checkbox" id="pickall" title="${esc(t.pickAll)}"></th>
        ${column("name", t.device)}${column("model", t.model)}${column("gateway", t.gateway)}${column("status", t.status)}
        ${column("seen", t.lastSeen)}${column("battery", t.battery)}${column("signal", t.signal)}${column("loss", t.loss)}${column("interval", t.interval)}${column("command", t.command)}</tr></thead>
      <tbody>${this.shown().map((d) => {
        const key = this.key(d);
        const on = this.selected.has(key);
        const glyph = icon(modelIcon(this.model(d).name));
        const avatar = d.device_id
          ? `<a class="avatar nav" href="/config/devices/device/${esc(d.device_id)}" title="${esc(t.openDevice)}">${glyph}</a>`
          : `<span class="avatar">${glyph}</span>`;
        return `<tr class="${d.active ? "" : "off"} ${d.sends ? "pick" : ""} ${on ? "sel" : ""}" data-key="${esc(key)}" ${d.sends ? "" : `title="${esc(t.noSend)}"`}>
          <td class="check"><input type="checkbox" ${on ? "checked" : ""} ${d.sends ? "" : "disabled"}></td>
          <td><span class="dev">${avatar}
            <span><span class="name">${esc(d.name)}</span><div class="mono">${esc(d.dev_eui)}</div></span></span></td>
          <td><span class="chip">${esc(this.model(d).name || d.model_id)}</span></td>
          <td>${this.gatewayCell(d)}</td>
          <td><span class="dot ${d.active ? "ok" : "bad"}">${esc(d.active ? t.online : t.offline)}</span></td>
          <td>${esc(this.ago(d.last_seen))}</td>
          <td>${this.battery(d.battery)}</td>
          <td>${this.signal(d)}</td>
          <td>${this.loss(d.packet_loss)}</td>
          <td>${this.intervalCell(d)}</td>
          <td>${this.pill(d.command.state)}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
    box.scrollTop = top;
    const pickable = this.pickable();
    const picked = pickable.filter((d) => this.selected.has(this.key(d))).length;
    const all = this.$("pickall");
    all.checked = pickable.length > 0 && picked === pickable.length;
    all.indeterminate = picked > 0 && picked < pickable.length;
    all.disabled = !pickable.length;
    all.onchange = () => {
      const every = picked === pickable.length;
      pickable.forEach((d) => { if (every) this.selected.delete(this.key(d)); else this.selected.add(this.key(d)); });
      this.selectionChanged();
    };
    box.querySelectorAll("th.sort").forEach((th) => {
      th.onclick = () => {
        const key = th.dataset.sort;
        this.sort = { key, dir: this.sort.key === key ? -this.sort.dir : 1 };
        this.renderRows();
      };
    });
    box.querySelectorAll("tr.pick").forEach((row) => {
      row.onclick = (e) => {
        if (e.target.closest("a")) return; // a link leaves the page instead
        const key = row.dataset.key;
        if (this.selected.has(key)) this.selected.delete(key); else this.selected.add(key);
        this.selectionChanged();
      };
    });
  }

  selectionChanged() {
    this.preview = null;
    this.results = null;
    this.renderRows();
    this.updateCount();
    this.renderSettings();
  }

  // ---- settings -----------------------------------------------------------

  control(id, f, known) {
    const value = this.form[id] ?? "";
    if (f.type === "NUMBER") {
      const range = [f.min !== undefined ? `min="${f.min}"` : "", f.max !== undefined ? `max="${f.max}"` : ""].join(" ");
      const hint = known && known.raw !== undefined ? known.raw : "–";
      return `<span class="numbox"><input type="number" step="any" ${range} data-num="${esc(id)}" value="${esc(value)}" placeholder="${esc(hint)}">
        <span class="unit">${esc(f.unit || "")}</span></span>`;
    }
    const options = this.optionNames(f);
    const text = options.map(([, name]) => name).join("");
    if (options.length <= 4 && text.length <= 36) {
      return `<span class="seg">${options.map(([raw, name]) =>
        `<button data-seg="${esc(id)}" data-val="${esc(raw)}" class="${String(value) === raw ? "on" : ""} ${known && known.raw === raw ? "cur" : ""}">${esc(name)}</button>`).join("")}</span>`;
    }
    return `<select data-sel="${esc(id)}"><option value="">${esc(this.t.choose)}</option>${options.map(([raw, name]) =>
      `<option value="${esc(raw)}" ${String(value) === raw ? "selected" : ""}>${esc(name)}</option>`).join("")}</select>`;
  }

  helpBox(id, f) {
    const t = this.t;
    const text = explainField(id, f, this.lang);
    const facts = [[t.field, `<span class="mono" style="font-size:12px">${esc(id)}</span>`]];
    if (f.min !== undefined || f.max !== undefined) facts.push([t.range, esc(`${f.min ?? "…"} – ${f.max ?? "…"}${f.unit ? ` ${f.unit}` : ""}`)]);
    // "default" is the value Milesight's codec gives as an example, not a
    // factory setting the device is known to have.
    if (!blank(f.default)) {
      facts.push([t.example, `${esc(this.valueText(f, f.default))} · <button class="link" data-default="${esc(id)}">${esc(t.useDefault)}</button>`]);
    }
    if (f.only) facts.push([t.types, esc(f.only.join(", "))]);
    return `<div class="help">${text ? `<p>${esc(text)}</p>` : ""}<dl>${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl></div>`;
  }

  setRow(id, f, label) {
    const t = this.t;
    const known = this.known(id, f);
    let hint = "";
    if (known && known.mixed) hint = t.mixed;
    else if (known) hint = `${t.now}: ${this.valueText(f, known.raw)}${known.src && known.src !== "current" ? ` · ${t.src[known.src] || known.src}` : ""}`;
    else if (!blank(f.default)) hint = `${t.example}: ${this.valueText(f, f.default)}`;
    const changed = !blank(this.form[id]);
    const open = this.helpOpen.has(id);
    return `<div class="set ${changed ? "changed" : ""}" data-row="${esc(id)}" data-text="${esc(`${f.name || ""} ${id}`)}">
      <span class="label"><b>${esc(label || f.name || pretty(id))}</b><small>${esc(hint)}</small>
        ${f.only ? `<span class="only">${f.only.map((name) => `<span class="chip">${esc(name)}</span>`).join("")}</span>` : ""}</span>
      ${this.control(id, f, known)}
      <span class="q"><button class="tool ${open ? "open" : ""}" data-help="${esc(id)}" aria-label="${esc(t.help)}">${icon("mdi:help-circle-outline")}</button>
        ${open ? "" : `<span class="tip" role="tooltip">${esc(explainField(id, f, this.lang))}</span>`}</span>
      <button class="tool undo" data-undo="${esc(id)}" title="${esc(t.unchanged)}">${icon("mdi:close")}</button>
      ${open ? this.helpBox(id, f) : ""}
    </div>`;
  }

  // The settings in groups: the ones every device has by their ID's prefix,
  // then the ones only some of the selection have.
  groups(settings) {
    const t = this.t;
    const rank = (id) => { const i = FIRST.indexOf(id); return i < 0 ? FIRST.length : i; };
    const byName = ([a, fa], [b, fb]) => rank(a) - rank(b) || String(fa.name || a).localeCompare(String(fb.name || b));
    const prefix = (id) => (id.includes(".") ? id.split(".")[0] : "");
    const shared = Object.entries(settings).filter(([, f]) => !f.only);
    const sizes = {};
    shared.forEach(([id]) => { sizes[prefix(id)] = (sizes[prefix(id)] || 0) + 1; });
    const sets = {};
    for (const entry of shared) {
      const p = prefix(entry[0]);
      (sets[p && sizes[p] > 1 ? p : ""] ||= []).push(entry);
    }
    const out = Object.keys(sets).sort((a, b) => (a === "" ? -1 : b === "" ? 1 : a.localeCompare(b))).map((name) => {
      const rows = sets[name].sort(byName);
      // "X (A)", "X (B)" under one prefix: the group is X, the rows A and B.
      const parts = rows.map(([, f]) => splitName(f.name));
      const head = name && parts.every((p) => p && p[0] === parts[0][0]) ? parts[0][0] : null;
      return {
        title: name ? head || pretty(name) : t.general,
        rows: rows.map(([id, f], i) => [id, f, head ? parts[i][1] : null]),
      };
    });
    const partial = Object.entries(settings).filter(([, f]) => f.only).sort(byName);
    if (partial.length) out.push({ title: t.partial, rows: partial.map(([id, f]) => [id, f, null]) });
    return out;
  }

  renderSettings() {
    const t = this.t;
    const chosen = this.chosen();
    const { settings, commands } = this.fields();
    // What the selection no longer has leaves the form.
    for (const id of Object.keys(this.form)) if (!settings[id] && !commands[id]) delete this.form[id];
    const types = {};
    chosen.forEach((d) => { const name = this.model(d).name || d.model_id; types[name] = (types[name] || 0) + 1; });
    const head = `<div class="head"><h2>${icon("mdi:tune-variant")}${esc(t.settings)} <small>${chosen.length ? esc(t.settingsFor(chosen.length)) : ""}</small></h2>
      <span class="tags">${Object.entries(types).map(([name, n]) => `<span class="chip">${n}× ${esc(name)}</span>`).join("")}</span>
      <span class="grow"></span>
      ${Object.keys(settings).length > 8 ? `<span class="search">${icon("mdi:magnify")}<input id="find" type="search" placeholder="${esc(t.findSetting)}" value="${esc(this.find)}"></span>` : ""}</div>`;
    const box = this.$("settings");
    if (!chosen.length || (!Object.keys(settings).length && !Object.keys(commands).length)) {
      box.innerHTML = `${head}<div class="body"><div class="empty">${icon("mdi:cursor-default-click-outline")}${esc(chosen.length ? t.noCommon : t.noneSelected)}</div></div>`;
      return;
    }
    const groups = this.groups(settings);
    box.innerHTML = `${head}<div class="body">
      ${groups.map((group) => `<div class="group" data-group>
        ${groups.length > 1 ? `<h3>${esc(group.title)}</h3>` : ""}
        <div class="sets">${group.rows.map(([id, f, label]) => this.setRow(id, f, label)).join("")}</div></div>`).join("")}
      ${Object.keys(commands).length ? `<div class="group"><h3>${esc(t.commands)}</h3><div class="cmds">${Object.entries(commands).map(([id, f]) =>
        `<button data-cmd="${esc(id)}" class="${this.form[id] ? "on" : ""}" title="${esc([explainField(id, f, this.lang), f.only ? f.only.join(", ") : ""].filter(Boolean).join(" · "))}">${icon(this.form[id] ? "mdi:check" : "mdi:play-outline")}${esc(f.name || pretty(id))}</button>`).join("")}</div></div>` : ""}
      <div id="outcome"></div>
      <div id="actions" class="actions"></div>
    </div>`;
    this.bindSettings(settings);
    this.applyFind();
    this.renderActions();
  }

  bindSettings(settings) {
    const box = this.$("settings");
    const touched = (id) => {
      this.preview = null;
      this.results = null;
      const row = box.querySelector(`[data-row="${CSS.escape(id)}"]`);
      if (row) row.classList.toggle("changed", !blank(this.form[id]));
      this.renderActions();
    };
    const find = this.$("find");
    if (find) find.oninput = (e) => { this.find = e.target.value; this.applyFind(); };
    box.querySelectorAll("[data-num]").forEach((el) => {
      el.oninput = () => { this.form[el.dataset.num] = el.value; touched(el.dataset.num); };
    });
    box.querySelectorAll("[data-sel]").forEach((el) => {
      el.onchange = () => { this.form[el.dataset.sel] = el.value; touched(el.dataset.sel); };
    });
    box.querySelectorAll("[data-seg]").forEach((el) => {
      el.onclick = () => {
        const id = el.dataset.seg;
        // A second click on the chosen value takes the change back.
        this.form[id] = String(this.form[id]) === el.dataset.val ? "" : el.dataset.val;
        box.querySelectorAll(`[data-seg="${CSS.escape(id)}"]`).forEach((b) => b.classList.toggle("on", b.dataset.val === String(this.form[id])));
        touched(id);
      };
    });
    box.querySelectorAll("[data-cmd]").forEach((el) => {
      el.onclick = () => {
        const id = el.dataset.cmd;
        this.form[id] = this.form[id] ? "" : 1;
        el.classList.toggle("on", Boolean(this.form[id]));
        el.querySelector("ha-icon").setAttribute("icon", this.form[id] ? "mdi:check" : "mdi:play-outline");
        touched(id);
      };
    });
    box.querySelectorAll("[data-undo]").forEach((el) => {
      el.onclick = () => { delete this.form[el.dataset.undo]; this.preview = null; this.renderSettings(); };
    });
    box.querySelectorAll("[data-help]").forEach((el) => {
      el.onclick = () => {
        const id = el.dataset.help;
        if (this.helpOpen.has(id)) this.helpOpen.delete(id); else this.helpOpen.add(id);
        this.renderSettings();
      };
    });
    box.querySelectorAll("[data-default]").forEach((el) => {
      el.onclick = () => {
        const id = el.dataset.default;
        this.form[id] = String(settings[id].default);
        this.preview = null;
        this.results = null;
        this.renderSettings();
      };
    });
  }

  applyFind() {
    const q = this.find.trim().toLowerCase();
    this.$("settings").querySelectorAll("[data-group]").forEach((group) => {
      let any = false;
      group.querySelectorAll("[data-row]").forEach((row) => {
        const hit = !q || row.dataset.text.toLowerCase().includes(q);
        row.style.display = hit ? "" : "none";
        any = any || hit;
      });
      group.style.display = any ? "" : "none";
    });
  }

  renderActions() {
    const t = this.t;
    const box = this.$("actions");
    if (!box) return;
    const items = this.items();
    const count = new Set(items.flatMap((i) => Object.keys(i.changes))).size;
    const names = Object.fromEntries(this.data.devices.map((d) => [d.dev_eui, d.name]));
    const rows = this.results || this.preview;
    // The bar sticks to the window's lower edge inside the card; the result
    // stands outside it, or a long one would cover the settings.
    this.$("outcome").innerHTML = rows ? `<div class="outcome"><table><thead><tr><th>${esc(t.device)}</th><th>${esc(this.results ? t.result : t.bytes)}</th></tr></thead><tbody>
        ${rows.map((r) => `<tr><td>${esc(names[r.dev_eui] || r.dev_eui)}<div class="mono">${esc(r.dev_eui)}</div></td>
          <td>${r.error ? `<span class="bad">${esc(r.error)}</span>` : r.ok ? `<span class="ok">${icon("mdi:check")} ${esc(t.sentOk)}</span>` : `<span class="mono" style="font-size:13px">${esc(r.hex)}</span>`}</td></tr>`).join("")}
      </tbody></table></div>` : "";
    box.innerHTML = `<span class="grow ${items.length ? "some" : ""}">${esc(items.length ? t.summary(count, items.length) : t.nothing)}</span>
        <button id="reset" ${Object.values(this.form).some((v) => !blank(v)) ? "" : "disabled"}>${esc(t.reset)}</button>
        <button id="preview" ${items.length && !this.busy ? "" : "disabled"}>${esc(t.preview)}</button>
        <button id="send" class="primary" ${items.length && !this.busy ? "" : "disabled"}>${esc(this.busy === "send" ? t.sending : t.send)}</button>`;
    this.$("reset").onclick = () => { this.form = {}; this.preview = null; this.results = null; this.renderSettings(); };
    this.$("preview").onclick = () => this.run("preview");
    this.$("send").onclick = () => this.run("send");
  }

  async run(type) {
    const items = this.items();
    if (!items.length || this.busy) return;
    this.busy = type;
    this.renderActions();
    try {
      const res = await this._hass.callWS({ type: `milesight_gateway/${type}`, items });
      if (type === "preview") this.preview = res.results;
      else {
        this.results = res.results;
        this.preview = null;
        this.form = {};
      }
    } catch (err) {
      this.results = items.map((i) => ({ dev_eui: i.dev_eui, error: err.message || String(err) }));
    }
    this.busy = false;
    if (type === "send") this.renderSettings();
    else this.renderActions();
    // The result stands at the end of the settings, mostly below the window.
    this.$("outcome").scrollIntoView({ block: "center", behavior: "smooth" });
    if (type === "send") await this.load();
  }

  // ---- log ----------------------------------------------------------------

  pairs(record, values) {
    const device = this.data.devices.find((d) => d.dev_eui === record.dev_eui);
    const model = device ? this.model(device) : {};
    const fields = { ...(model.settings || {}), ...(model.commands || {}) };
    return Object.entries(values || {}).map(([id, raw]) => {
      const f = fields[id];
      return `<span class="kv"><span>${esc(f ? f.name || pretty(id) : id)}:</span> ${esc(f ? this.valueText(f, num(raw)) : num(raw))}</span>`;
    }).join("");
  }

  // The commands of the newest send: those logged within two minutes before
  // the newest one (a send of 100 devices takes about 20 s).
  batch() {
    const commands = this.records.filter((r) => r.kind !== "join");
    if (!commands.length) return [];
    const newest = Math.max(...commands.map((r) => Date.parse(r.t)));
    return commands.filter((r) => Date.parse(r.t) >= newest - 120000);
  }

  // What a count in the log's head stands for; a click shows just those.
  logFilters() {
    const t = this.t.tally;
    const waiting = ["pending", "retrying", "transmitted"];
    return [
      { id: "waiting", label: t.waiting, tone: "warn", states: waiting, batch: true },
      { id: "acked", label: t.acked, tone: "ok", states: ["acknowledged"], batch: true },
      { id: "confirmed", label: t.confirmed, tone: "ok", states: ["confirmed"], batch: true },
      { id: "unconfirmed", label: t.unconfirmed, tone: "muted", states: ["sent"], batch: true },
      { id: "failed", label: t.failed, tone: "bad", states: ["failed", "differs"], batch: true },
      { id: "open", label: t.open, tone: "warn", states: waiting, batch: false },
    ];
  }

  matching(filter) {
    const pool = filter.batch ? this.batch() : this.records.filter((r) => r.kind !== "join");
    return pool.filter((r) => filter.states.includes(r.state));
  }

  tally() {
    const t = this.t.tally;
    const batch = this.batch();
    if (!batch.length) return "";
    const pills = this.logFilters().map((f) => {
      const n = this.matching(f).length;
      const on = this.logFilter === f.id;
      if (!n && !on) return "";
      return `${f.batch ? "" : `<span class="sub">·</span>`}<button class="pill ${f.tone} ${on ? "on" : ""}" data-filter="${f.id}" aria-pressed="${on}" title="${esc(on ? t.showAll : t.showOnly)}">${n} ${esc(f.label)}</button>`;
    }).join("");
    return `<span class="tally"><span class="sub">${esc(t.title)}:</span>
      <span class="pill muted">${batch.length} ${esc(t.sent)}</span>${pills}</span>`;
  }

  logSortValue(r, key) {
    switch (key) {
      case "device": return r.name || r.dev_eui;
      case "command": return JSON.stringify(r.command || {});
      case "result": return r.state || "";
      case "attempts": return r.attempts ?? null;
      default: return Date.parse(r.t);
    }
  }

  logRows() {
    const filter = this.logFilters().find((f) => f.id === this.logFilter);
    const rows = filter ? this.matching(filter) : [...this.records];
    const { key, dir } = this.logSort;
    return rows.sort((a, b) => {
      const x = this.logSortValue(a, key), y = this.logSortValue(b, key);
      if (blank(x) || blank(y)) return blank(x) === blank(y) ? 0 : blank(x) ? 1 : -1;
      const order = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
      return order * dir || Date.parse(b.t) - Date.parse(a.t);
    });
  }

  renderLog() {
    const t = this.t;
    const box = this.$("log");
    const scroll = box.querySelector(".scroll");
    const top = scroll ? scroll.scrollTop : 0;
    const column = (key, label) => {
      const on = this.logSort.key === key;
      return `<th class="sort ${on ? "sorted" : ""}" data-logsort="${key}">${esc(label)} ${on ? icon(this.logSort.dir > 0 ? "mdi:arrow-up" : "mdi:arrow-down") : ""}</th>`;
    };
    const rows = this.logRows();
    box.innerHTML = `<div class="head"><h2>${icon("mdi:history")}${esc(t.log)}</h2>${this.tally()}</div>
      ${this.records.length ? `<div class="scroll" style="max-height:40vh"><table>
        <thead><tr>${column("time", t.time)}${column("device", t.device)}${column("command", t.command)}${column("result", t.result)}${column("attempts", t.attempts)}</tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td title="${esc(new Date(r.t).toLocaleString())}">${esc(this.ago(r.t))}</td>
          <td>${esc(r.name || r.dev_eui)}<div class="sub">${esc(r.gateway || "")}</div></td>
          <td>${r.kind === "join" ? `<span class="muted">${icon("mdi:lan-connect")} ${esc(t.join)}</span>` : this.pairs(r, r.command)}</td>
          <td>${r.kind === "join" ? "" : this.pill(r.state)}${r.reported && Object.keys(r.reported).length ? `<div class="sub">${this.pairs(r, r.reported)}</div>` : ""}</td>
          <td>${r.attempts ?? ""}</td>
        </tr>`).join("")}</tbody></table></div>` : `<div class="body"><div class="empty">${esc(t.emptyLog)}</div></div>`}`;
    const fresh = box.querySelector(".scroll");
    if (fresh) fresh.scrollTop = top;
    box.querySelectorAll("[data-filter]").forEach((el) => {
      el.onclick = () => {
        this.logFilter = this.logFilter === el.dataset.filter ? null : el.dataset.filter;
        this.renderLog();
      };
    });
    box.querySelectorAll("[data-logsort]").forEach((th) => {
      th.onclick = () => {
        const key = th.dataset.logsort;
        // Newest first is the order a log starts in.
        this.logSort = { key, dir: this.logSort.key === key ? -this.logSort.dir : key === "time" ? -1 : 1 };
        this.renderLog();
      };
    });
  }

  // ---- import -------------------------------------------------------------

  async importChoices(entryId) {
    this.imp = { ...this.imp, entry_id: entryId, choices: null, error: null, results: null };
    if (entryId) {
      try {
        const c = await this._hass.callWS({ type: "milesight_gateway/import_choices", entry_id: entryId });
        this.imp.choices = c;
        this.imp.application = (c.applications[0] || {}).id || "";
        this.imp.profile = (c.profiles[0] || {}).id || "";
      } catch (err) {
        this.imp.error = err.message || String(err);
      }
    }
    this.renderImport();
  }

  async runImport() {
    const rows = this.imp.text.split(/\n/).map((l) => l.trim()).filter(Boolean).map((l) => {
      const [dev_eui, name, app_key] = l.split(/[;,\t]/).map((x) => (x || "").trim());
      return { dev_eui, name, app_key };
    });
    if (!rows.length) return;
    try {
      const res = await this._hass.callWS({
        type: "milesight_gateway/import", entry_id: this.imp.entry_id,
        application_id: this.imp.application, profile_id: this.imp.profile, codec_id: this.imp.codec, rows,
      });
      this.imp.results = res.results;
      this.imp.error = null;
    } catch (err) {
      this.imp.error = err.message || String(err);
    }
    this.renderImport();
    this.load();
  }

  renderImport() {
    const t = this.t, imp = this.imp, gateways = this.data.gateways || [];
    const box = this.$("import");
    this._importDrawn = true;
    this._importGateways = gateways.length;
    box.style.display = gateways.length ? "" : "none";
    const pick = (id, list, current, extra) => `<select id="${id}">${extra || ""}${(list || []).map((x) =>
      `<option value="${esc(x.id)}" ${x.id === current ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>`;
    const c = imp.choices;
    box.innerHTML = `<summary class="head"><h2>${icon("mdi:plus-circle-outline")}${esc(t.import)}</h2>${icon("mdi:chevron-down", "chev")}</summary>
      <div class="body">
        <div class="form"><label>${esc(t.pickGateway)}${pick("imp-gw", gateways.map((g) => ({ id: g.entry_id, name: g.title })), imp.entry_id, "<option value=''>–</option>")}</label>
        ${c ? `<label>${esc(t.application)}${pick("imp-app", c.applications, imp.application)}</label>
          <label>${esc(t.profile)}${pick("imp-profile", c.profiles, imp.profile)}</label>
          <label>${esc(t.codec)}${pick("imp-codec", c.codecs, imp.codec, `<option value="auto" ${imp.codec === "auto" ? "selected" : ""}>${esc(t.auto)}</option>`)}</label>` : ""}</div>
        ${imp.error ? `<p class="error">${esc(imp.error)}</p>` : ""}
        ${c ? `<div class="sub" style="margin-bottom:4px">${esc(t.rows)}</div>
          <textarea id="imp-text" rows="5" spellcheck="false">${esc(imp.text)}</textarea>
          <div style="margin-top:12px"><button id="imp-run" class="primary">${esc(t.doImport)}</button></div>
          ${imp.results ? `<div class="outcome"><table><tbody>${imp.results.map((r) => `<tr><td class="mono" style="font-size:13px">${esc(r.dev_eui)}</td><td>${r.ok
            ? `<span class="ok">${icon("mdi:check")} ${esc(t.created)}</span>${r.codec === "0" ? ` <span class="warn">(${esc(t.noCodec)})</span>` : ""}`
            : `<span class="bad">${esc(r.error)}</span>`}</td></tr>`).join("")}</tbody></table></div>` : ""}` : ""}
      </div>`;
    const on = (id, ev, fn) => { const el = this.$(id); if (el) el[ev] = fn; };
    on("imp-gw", "onchange", (e) => this.importChoices(e.target.value));
    on("imp-app", "onchange", (e) => { this.imp.application = e.target.value; });
    on("imp-profile", "onchange", (e) => { this.imp.profile = e.target.value; });
    on("imp-codec", "onchange", (e) => { this.imp.codec = e.target.value; });
    on("imp-text", "oninput", (e) => { this.imp.text = e.target.value; });
    on("imp-run", "onclick", () => this.runImport());
  }
}

// A restart gives the script a new address; the page may load it twice.
if (!customElements.get("milesight-devices-panel")) {
  customElements.define("milesight-devices-panel", MilesightDevicesPanel);
}
