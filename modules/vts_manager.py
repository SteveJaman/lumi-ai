import asyncio
import json
import logging
import pyvts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VTSManager")


class VTSManager:
    def __init__(self, config_path="./params.json",
                 token_path="./vtubeStudio_test_token.txt"):
        self.config_path = config_path
        self.plugin_info = {
            "plugin_name": "Lumi VTS Controller",
            "developer": "Anthony",
            "authentication_token_path": token_path,
        }
        self.vts = pyvts.vts(plugin_info=self.plugin_info)
        self.custom_params = []
        self.active_values = {}
        self._streaming_task = None
        self._is_running = False

        # Filled in during connect_and_register()
        self.existing_params = set()    # names the model already exposes
        self.created_params = set()     # names we successfully created

    # ---------------------------------------------------------------
    # Config
    # ---------------------------------------------------------------
    def load_config(self):
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.custom_params = data.get("custom_params", [])
        for param in self.custom_params:
            self.active_values[param["parameterName"]] = param.get(
                "defaultValue", 0.0
            )
        logger.info(f"Loaded {len(self.custom_params)} params from "
                    f"{self.config_path}")

    # ---------------------------------------------------------------
    # Connection / auth
    # ---------------------------------------------------------------
    async def connect_and_register(self):
        await self.vts.connect()
        await self.vts.request_authenticate_token()
        if not await self.vts.request_authenticate():
            logger.error("Authentication failed — token invalid or revoked.")
            raise PermissionError(
                "VTube Studio authentication failed. Delete the token "
                "file and re-run to re-authorize."
            )
        logger.info("VTS authenticated.")

        self.load_config()

        # 1) Ask VTS what the loaded model already exposes.
        self.existing_params = await self._fetch_input_params()
        if not self.existing_params:
            logger.warning(
                "Could not read the model's input parameter list. "
                "Will attempt creation for every configured param."
            )
        else:
            logger.info(
                f"Model exposes {len(self.existing_params)} input params."
            )

        # 2) Create only the params that don't already exist.
        for param in self.custom_params:
            name = param["parameterName"]
            if name in self.existing_params:
                continue  # built-in or already-defined custom — leave it

            payload = {
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": f"Register_{name}",
                "messageType": "ParameterCreationRequest",
                "data": {
                    "parameterName": name,
                    "explanation": param.get("explanation", ""),
                    "min": float(param["min"]),
                    "max": float(param["max"]),
                    "defaultValue": float(param["defaultValue"]),
                },
            }
            try:
                resp = await self.vts.request(payload)
            except Exception as e:
                logger.error(f"  ✗ {name}: request raised {e}")
                continue

            if not resp or resp.get("messageType") == "APIError":
                msg = (resp or {}).get("data", {}).get("message", "unknown")
                logger.error(f"  ✗ {name}: {msg}")
            else:
                self.created_params.add(name)
                logger.info(f"  ✓ created {name}")

        # 3) Sanity check — every configured name must exist somewhere.
        missing = [
            p["parameterName"] for p in self.custom_params
            if p["parameterName"] not in self.existing_params
            and p["parameterName"] not in self.created_params
        ]
        if missing:
            logger.error(
                f"Params that are neither built-in nor creatable: {missing}"
            )
            logger.error(
                "These will be silently ignored during injection. "
                "Check your model's Parameter Settings page."
            )
        else:
            logger.info(
                "All configured params are available for injection."
            )

    async def _fetch_input_params(self):
        """Query VTS for the input parameter names the loaded model exposes.

        Returns a set of names. Empty set on failure.
        """
        try:
            request = self.vts.vts_request.BaseRequest(
                "InputParameterListRequest", {}
            )
            response = await self.vts.request(request)
        except Exception as e:
            logger.warning(f"InputParameterListRequest failed: {e}")
            return set()

        if not response or response.get("messageType") == "APIError":
            return set()

        data = response.get("data", {}) or {}
        names = set()
        for entry in (data.get("defaultParameters") or []):
            n = entry.get("name")
            if n:
                names.add(n)
        for entry in (data.get("customParameters") or []):
            n = entry.get("name")
            if n:
                names.add(n)
        return names

    # ---------------------------------------------------------------
    # Parameter control
    # ---------------------------------------------------------------
    def set_parameter(self, param_name: str, value: float):
        if param_name in self.active_values:
            self.active_values[param_name] = value
        else:
            logger.warning(
                f"Parameter '{param_name}' not in config — ignored."
            )

    # ---------------------------------------------------------------
    # Streaming loop
    # ---------------------------------------------------------------
    async def _stream_loop(self, fps: int = 60):
        interval = 1.0 / fps
        self._is_running = True
        logger.info(f"Streaming loop started at {fps} FPS.")

        try:
            while self._is_running:
                param_list = [
                    {"id": name, "value": float(val), "weight": 1.0}
                    for name, val in self.active_values.items()
                ]

                inject_payload = {
                    "apiName": "VTubeStudioPublicAPI",
                    "apiVersion": "1.0",
                    "requestID": "LumiStream",
                    "messageType": "InjectParameterDataRequest",
                    "data": {
                        "faceFound": True,
                        "mode": "set",
                        "parameterValues": param_list,
                    },
                }

                try:
                    resp = await self.vts.request(inject_payload)
                    if resp and resp.get("messageType") == "APIError":
                        msg = resp.get("data", {}).get("message", "unknown")
                        # Log once per second max so we don't flood
                        if int(asyncio.get_event_loop().time()) % 2 == 0:
                            logger.warning(f"Inject rejected: {msg}")
                except Exception as e:
                    logger.warning(f"Inject request error: {e}")

                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Streaming loop canceled.")
            raise

    def start_streaming(self, fps: int = 60):
        if not self._streaming_task or self._streaming_task.done():
            self._streaming_task = asyncio.create_task(self._stream_loop(fps))

    async def stop_streaming(self):
        self._is_running = False
        if self._streaming_task:
            self._streaming_task.cancel()
            await asyncio.gather(self._streaming_task, return_exceptions=True)
            self._streaming_task = None

    async def reset_all(self):
        for param in self.custom_params:
            self.active_values[param["parameterName"]] = param.get(
                "defaultValue", 0.0
            )
        reset_list = [
            {"id": name, "value": float(val), "weight": 1.0}
            for name, val in self.active_values.items()
        ]
        payload = {
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": "LumiReset",
            "messageType": "InjectParameterDataRequest",
            "data": {
                "faceFound": True,
                "mode": "set",
                "parameterValues": reset_list,
            },
        }
        try:
            await self.vts.request(payload)
            logger.info("Parameters reset to defaults.")
        except Exception as e:
            logger.warning(f"Reset failed: {e}")

    async def close(self):
        await self.stop_streaming()
        await asyncio.sleep(0.2)
        await self.vts.close()
        logger.info("VTS connection closed.")