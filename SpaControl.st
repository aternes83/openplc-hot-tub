(*
  Hot tub / spa control for OpenPLC (IEC 61131-3 Structured Text)

  Plant:
    - Supply: 240 Vac
    - Heater: 5 kW (single contactor; ~20.8 A @ 240 V — size O/L and contactors per manufacturer)
    - Pump 1: two-speed (LOW and HIGH are mutually exclusive; never energize both)
    - Pump 2: single-speed
    - Pump 3: single-speed
    - Temperature: degrees Fahrenheit (sensor scaling in HMI/I/O must match °F)

  Map symbols to I/O in OpenPLC. Verify interlocks and limits with a qualified installer.
*)

FUNCTION_BLOCK FB_OnDelay
VAR_INPUT
    In : BOOL;
    Delay : TIME := T#5s;
END_VAR
VAR_OUTPUT
    Q : BOOL;
END_VAR
VAR
    ton : TON;
END_VAR

ton(IN := In, PT := Delay);
Q := ton.Q;

END_FUNCTION_BLOCK


FUNCTION_BLOCK FB_ThermostatHeat
(* Heater demand with deadband; heat only when enabled and all permissives are true. *)
VAR_INPUT
    Enable : BOOL;
    Temp_F : REAL;
    Setpoint_F : REAL;
    Hysteresis_F : REAL;
    FlowOK : BOOL;
    HighLimitOK : BOOL;
    FlowProven : BOOL;
    MinOnTimeElapsed : BOOL;
END_VAR
VAR_OUTPUT
    HeatOn : BOOL;
END_VAR
VAR
    spLow : REAL;
    spHigh : REAL;
END_VAR

spLow := Setpoint_F - (Hysteresis_F * 0.5);
spHigh := Setpoint_F + (Hysteresis_F * 0.5);

IF NOT Enable OR NOT FlowOK OR NOT HighLimitOK OR NOT FlowProven OR NOT MinOnTimeElapsed THEN
    HeatOn := FALSE;
ELSIF Temp_F < spLow THEN
    HeatOn := TRUE;
ELSIF Temp_F > spHigh THEN
    HeatOn := FALSE;
END_IF;

END_FUNCTION_BLOCK


PROGRAM SpaControl
VAR
    (* ---- Operator / panel inputs (map to %IX / %IW or HMI) ---- *)
    xSpaEnable : BOOL;           (* Master: spa may run *)
    xPumpRequest : BOOL;         (* Circulation / filter: run Pump1 LOW (and allow Pump2/3 per requests) *)
    xHeatRequest : BOOL;         (* Allow heating toward setpoint (requires flow) *)
    xPump1HighRequest : BOOL;    (* Pump 1 HIGH speed; forces Pump1 LOW off *)
    xPump2Request : BOOL;        (* Single-speed pump 2 *)
    xPump3Request : BOOL;        (* Single-speed pump 3 *)
    xJetsRequest : BOOL;         (* Optional diverter / jet valve, etc. *)
    xBlowerRequest : BOOL;
    xLightRequest : BOOL;
    xFlowSwitch : BOOL;          (* TRUE when adequate flow *)
    xHighLimitOK : BOOL;         (* TRUE when high-limit permissive (e.g. NC limit closed) *)
    xRemoteEStopOK : BOOL := TRUE; (* FALSE = trip; wire to physical e-stop or leave default TRUE *)
    rWaterTemp_F : REAL;         (* Water temperature, °F (scale analog input accordingly) *)
    rTempSetpoint_F : REAL := 100.0;
    rTempHysteresis_F : REAL := 2.0;
    rMaxSafeTemp_F : REAL := 108.0; (* software over-temp; set below mechanical limit *)

    (* ---- Outputs (map to %QX / contactors) ---- *)
    xPump1_Low : BOOL;           (* Pump 1 low-speed contactor *)
    xPump1_High : BOOL;          (* Pump 1 high-speed contactor *)
    xPump2 : BOOL;
    xPump3 : BOOL;
    xHeater : BOOL;              (* 5 kW @ 240 V *)
    xJets : BOOL;
    xBlower : BOOL;
    xLight : BOOL;
    xFault : BOOL;
    iFaultCode : INT;            (* 0=none, 1=no flow, 2=high limit, 3=overtemp, 4=estop *)

    (* ---- Internal ---- *)
    fbFlowProve : FB_OnDelay;
    fbPumpMinRun : FB_OnDelay;
    fbTherm : FB_ThermostatHeat;

    xAnyPump : BOOL;
    xPermissive : BOOL;
    tFlowProve : TIME := T#10s;
    tPumpPreheat : TIME := T#30s;
END_VAR

(* Master permissive *)
xPermissive := xSpaEnable AND xHighLimitOK AND xRemoteEStopOK AND (rWaterTemp_F <= rMaxSafeTemp_F);

(* Pump 1: HIGH overrides LOW (two-speed interlock) *)
xPump1_High := xPermissive AND xPump1HighRequest;
xPump1_Low := xPermissive AND (xPumpRequest OR xHeatRequest) AND NOT xPump1_High;

xPump2 := xPermissive AND xPump2Request;
xPump3 := xPermissive AND xPump3Request;

xAnyPump := xPump1_Low OR xPump1_High OR xPump2 OR xPump3;

fbFlowProve(In := xAnyPump AND xFlowSwitch, Delay := tFlowProve);
fbPumpMinRun(In := xAnyPump, Delay := tPumpPreheat);

fbTherm(
    Enable := xPermissive AND xHeatRequest,
    Temp_F := rWaterTemp_F,
    Setpoint_F := rTempSetpoint_F,
    Hysteresis_F := rTempHysteresis_F,
    FlowOK := xFlowSwitch,
    HighLimitOK := xHighLimitOK,
    FlowProven := fbFlowProve.Q,
    MinOnTimeElapsed := fbPumpMinRun.Q
);

xHeater := fbTherm.HeatOn;
xJets := xPermissive AND xJetsRequest AND xAnyPump;
xBlower := xPermissive AND xBlowerRequest;
xLight := xSpaEnable AND xLightRequest;

(* Faults: first matching condition sets code *)
xFault := FALSE;
iFaultCode := 0;

IF xSpaEnable AND NOT xRemoteEStopOK THEN
    xFault := TRUE;
    iFaultCode := 4;
ELSIF xSpaEnable AND NOT xHighLimitOK THEN
    xFault := TRUE;
    iFaultCode := 2;
ELSIF xSpaEnable AND (rWaterTemp_F > rMaxSafeTemp_F) THEN
    xFault := TRUE;
    iFaultCode := 3;
ELSIF xAnyPump AND fbFlowProve.Q AND NOT xFlowSwitch THEN
    xFault := TRUE;
    iFaultCode := 1;
END_IF;

IF xFault THEN
    xPump1_Low := FALSE;
    xPump1_High := FALSE;
    xPump2 := FALSE;
    xPump3 := FALSE;
    xHeater := FALSE;
    xJets := FALSE;
    xBlower := FALSE;
END_IF;

END_PROGRAM

CONFIGURATION Config0
RESOURCE Res0 ON PLC
    TASK MainTask(INTERVAL := T#100ms, PRIORITY := 0);
    PROGRAM Inst0 WITH MainTask : SpaControl;
END_RESOURCE
END_CONFIGURATION
