import hashlib
import unittest

from meshgram.radio_admin import (
    PUBLIC_CHANNEL_SECRET,
    RadioAdmin,
    RadioAdminError,
    RadioConflictError,
    RadioNotFoundError,
    channel_kind,
    hashtag_secret,
)
from meshgram.transport import RadioCommandError

SELF_KEY = "aa" * 32
CONTACT_KEY = "c3" * 32


class FakeRadio:
    """Stands in for MeshCoreTransport: keeps channels and contacts, records commands."""

    payload_limit = 140

    def __init__(self, max_channels=4):
        self.is_connected = True
        self.commands: list[tuple] = []
        self.unsupported: set[str] = {"get_stats_packets"}
        self.failing: set[str] = set()
        self.slots = {index: ("", bytes(16)) for index in range(max_channels)}
        self.slots[0] = ("Public", PUBLIC_CHANNEL_SECRET)
        self.device_info = {"model": "Heltec V3", "ver": "1.9.1", "max_channels": max_channels, "path_hash_mode": 0}
        self.device_self_info = {
            "name": "Base", "public_key": SELF_KEY, "adv_type": 1, "adv_lat": 45.0, "adv_lon": -73.0,
            "tx_power": 20, "max_tx_power": 22, "radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 8,
            "adv_loc_policy": 0, "manual_add_contacts": False, "multi_acks": 0,
            "telemetry_mode_base": 0, "telemetry_mode_loc": 0, "telemetry_mode_env": 0,
        }  # fmt: skip
        self.contacts = {CONTACT_KEY: {"public_key": CONTACT_KEY, "adv_name": "Phone", "type": 1, "last_advert": 100, "out_path_len": -1}}
        self.invalidated = False

    @property
    def max_channels(self):
        return self.device_info["max_channels"]

    @property
    def channel_slots(self):
        return [
            {"index": index, "name": name, "secret": secret, "hash": hashlib.sha256(secret).hexdigest()[:2]}
            for index, (name, secret) in sorted(self.slots.items())
        ]

    async def command(self, name, *args, **kwargs):
        if not self.is_connected:
            raise RadioCommandError("The radio isn't connected")
        if name in self.unsupported:
            raise RadioCommandError("the radio's firmware doesn't support this")
        self.commands.append((name, *args, *kwargs.values()))
        if name in self.failing:
            raise RadioCommandError("the radio rejected the value")
        if name == "set_channel":
            index, channel_name, secret = args
            self.slots[index] = (channel_name, secret)
        elif name == "remove_contact":
            self.contacts.pop(args[0])
        elif name == "set_name":
            self.device_self_info["name"] = args[0]
        elif name == "get_bat":
            return {"level": 4012}
        elif name == "get_stats_core":
            return {"battery_mv": 4012, "uptime_secs": 3600, "errors": 0, "queue_len": 0}
        elif name == "get_stats_radio":
            return {"noise_floor": -110, "last_rssi": -90, "last_snr": 7.5, "tx_air_secs": 10, "rx_air_secs": 50}
        elif name == "get_tuning":
            return {"rx_delay": 0, "airtime_factor": 1000}
        elif name == "get_time":
            return {"time": 0}
        return {}

    async def refresh_channels(self):
        self.commands.append(("refresh_channels",))

    async def refresh_contacts(self):
        self.commands.append(("refresh_contacts",))

    async def refresh_self_info(self):
        self.commands.append(("refresh_self_info",))
        return self.device_self_info

    def invalidate_connection(self):
        self.invalidated = True


class ChannelKindTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(channel_kind("Public", PUBLIC_CHANNEL_SECRET), "public")
        self.assertEqual(channel_kind("#local", hashtag_secret("#local")), "hashtag")
        self.assertEqual(channel_kind("#local", b"\x01" * 16), "private")
        self.assertEqual(channel_kind("", bytes(16)), "empty")
        # MeshCore's hashtag keys: SHA-256 of the name, "#" included.
        self.assertEqual(hashtag_secret("#test"), hashlib.sha256(b"#test").digest()[:16])


class RadioAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.radio = FakeRadio()
        self.admin = RadioAdmin(self.radio)

    # Overview / settings ----------------------------------------------------------

    async def test_overview_skips_what_the_firmware_lacks(self):
        overview = await self.admin.overview()
        self.assertTrue(overview["connected"])
        self.assertEqual(overview["identity"]["type"], "chat")
        self.assertEqual(overview["radio"]["sf"], 8)
        self.assertEqual(overview["battery"], {"level": 4012})
        self.assertEqual(set(overview["stats"]), {"core", "radio"})
        self.assertEqual(overview["tuning"], {"rx_delay": 0.0, "airtime_factor": 1.0})
        self.assertLess(overview["clock"]["drift_seconds"], -1_000_000)

        self.radio.is_connected = False
        self.assertEqual(await self.admin.overview(), {"connected": False})

    async def test_update_settings_validates_everything_first(self):
        bad = [
            {"name": ""},
            {"name": "x" * 32},
            {"lat": 45.0},
            {"lat": 91, "lon": 0},
            {"tx_power": 23},
            {"tx_power": True},
            {"radio": {"freq": 869.5, "bw": 100, "sf": 8, "cr": 8}},
            {"radio": {"freq": 869.5, "bw": 62.5, "sf": 13, "cr": 8}},
            {"telemetry_mode_env": 3},
            {"bogus": 1},
            {},
        ]
        for changes in bad:
            with self.subTest(changes=changes):
                with self.assertRaises(RadioAdminError):
                    await self.admin.update_settings({"name": "Fine", **changes} if "name" not in changes and changes else changes)
        self.assertEqual(self.radio.commands, [])

    async def test_update_settings_applies_in_order_and_refreshes(self):
        overview = await self.admin.update_settings(
            {
                "name": "Hilltop",
                "lat": 45.5,
                "lon": -73.6,
                "tx_power": 22,
                "radio": {"freq": 869.525, "bw": 250, "sf": 11, "cr": 5},
                "manual_add_contacts": True,
                "telemetry_mode_loc": 2,
                "tuning": {"rx_delay": 0.5, "airtime_factor": 2},
            }
        )
        names = [command[0] for command in self.radio.commands]
        self.assertEqual(
            names[:8],
            ["set_name", "set_coords", "set_tx_power", "set_radio", "set_manual_add_contacts", "set_telemetry_mode_loc", "set_tuning", "refresh_self_info"],
        )
        self.assertIn(("set_radio", 869.525, 250.0, 11, 5), self.radio.commands)
        self.assertIn(("set_tuning", 500, 2000), self.radio.commands)
        self.assertEqual(overview["identity"]["name"], "Hilltop")

    async def test_partial_failure_says_what_was_changed(self):
        self.radio.failing.add("set_tx_power")
        with self.assertRaisesRegex(RadioCommandError, r"TX power: .*\(name changed\)"):
            await self.admin.update_settings({"name": "Hilltop", "tx_power": 10})

    async def test_reboot_reconnects(self):
        with self.assertLogs("meshgram.radio_admin", "WARNING"):
            await self.admin.reboot()
        self.assertEqual(self.radio.commands, [("reboot",)])

    async def test_commands_need_the_radio(self):
        self.radio.is_connected = False
        with self.assertRaisesRegex(RadioCommandError, "isn't connected"):
            await self.admin.send_advert(True)
        with self.assertRaisesRegex(RadioCommandError, "isn't connected"):
            await self.admin.add_channel("#local")

    # Channels --------------------------------------------------------------------------

    async def test_add_hashtag_channel_by_name(self):
        channel = await self.admin.add_channel("#local")
        self.assertEqual(channel, {"index": 1, "name": "#local", "hash": hashlib.sha256(hashtag_secret("#local")).hexdigest()[:2], "kind": "hashtag"})
        self.assertIn(("set_channel", 1, "#local", hashtag_secret("#local")), self.radio.commands)
        with self.assertRaisesRegex(RadioConflictError, "already on slot 1"):
            await self.admin.add_channel("#local")
        with self.assertRaises(RadioAdminError):
            await self.admin.add_channel("#local", secret="00" * 16)
        with self.assertRaises(RadioAdminError):
            await self.admin.add_channel("#two words")

    async def test_add_private_channel_with_given_or_new_key(self):
        shared = await self.admin.add_channel("Friends", secret="0102030405060708090A0B0C0D0E0F10", index=3)
        self.assertEqual((shared["index"], shared["kind"]), (3, "private"))
        self.assertEqual(self.radio.slots[3][1], bytes.fromhex("0102030405060708090a0b0c0d0e0f10"))
        fresh = await self.admin.add_channel("Family")
        self.assertEqual((fresh["index"], fresh["kind"]), (1, "private"))
        self.assertNotEqual(self.radio.slots[1][1], bytes(16))
        for bad_key in ("abcd", "zz" * 16, "00" * 16):
            with self.assertRaises(RadioAdminError):
                await self.admin.add_channel("Other", secret=bad_key)

    async def test_add_public_channel_and_full_radio(self):
        self.radio.slots[0] = ("", bytes(16))
        public = await self.admin.add_channel("public")
        self.assertEqual((public["index"], public["name"], public["kind"]), (0, "Public", "public"))
        await self.admin.add_channel("#a")
        await self.admin.add_channel("#b")
        await self.admin.add_channel("#c")
        with self.assertRaisesRegex(RadioConflictError, "All 4 channel slots are in use"):
            await self.admin.add_channel("#d")
        with self.assertRaisesRegex(RadioConflictError, "Slot 2 already has"):
            await self.admin.add_channel("#d", index=2)
        with self.assertRaises(RadioNotFoundError):
            await self.admin.add_channel("#d", index=9)

    async def test_rename_rekey_and_remove(self):
        await self.admin.add_channel("Friends", secret="11" * 16)
        renamed = await self.admin.update_channel(1, "Pals")
        self.assertEqual(renamed["name"], "Pals")
        self.assertEqual(self.radio.slots[1], ("Pals", b"\x11" * 16))
        await self.admin.update_channel(1, "Pals", secret="22" * 16)
        self.assertEqual(self.radio.slots[1][1], b"\x22" * 16)
        with self.assertRaisesRegex(RadioConflictError, "slot 0"):
            await self.admin.update_channel(1, "Pals", secret=PUBLIC_CHANNEL_SECRET.hex())

        await self.admin.remove_channel(1)
        self.assertEqual(self.radio.slots[1], ("", bytes(16)))
        with self.assertRaises(RadioNotFoundError):
            await self.admin.remove_channel(1)
        with self.assertRaises(RadioNotFoundError):
            await self.admin.update_channel(2, "Nobody")

    async def test_list_channels_hides_keys_unless_asked(self):
        await self.admin.add_channel("#local")
        listed = self.admin.list_channels(include_secrets=False)
        self.assertEqual(listed["max_channels"], 4)
        self.assertEqual([(c["index"], c["kind"]) for c in listed["channels"]], [(0, "public"), (1, "hashtag")])
        self.assertNotIn("secret", listed["channels"][0])
        self.assertEqual(self.admin.list_channels(include_secrets=True)["channels"][1]["secret"], hashtag_secret("#local").hex())

    # Contacts ---------------------------------------------------------------------------

    async def test_contacts(self):
        contacts = self.admin.list_contacts()
        self.assertEqual(contacts[0]["name"], "Phone")
        self.assertEqual((contacts[0]["type"], contacts[0]["path_len"]), ("chat", -1))
        await self.admin.reset_contact_path(CONTACT_KEY.upper())
        await self.admin.remove_contact(CONTACT_KEY)
        self.assertEqual(self.radio.contacts, {})
        self.assertIn(("reset_path", CONTACT_KEY), self.radio.commands)
        with self.assertRaises(RadioNotFoundError):
            await self.admin.remove_contact(CONTACT_KEY)


if __name__ == "__main__":
    unittest.main()
