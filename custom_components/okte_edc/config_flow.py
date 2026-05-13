"""Config and options flow for the OKTE EDC integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CLEANUP_ARCHIVE,
    CLEANUP_DELETE,
    CLEANUP_LEAVE,
    CONF_EICS,
    CONF_FOLDER,
    CONF_USE_SSL,
    DEFAULT_ARCHIVE_FOLDER,
    DEFAULT_DELETE_AFTER_DAYS,
    DEFAULT_EMAIL_CLEANUP,
    DEFAULT_FOLDER,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_TIMEZONE,
    DEFAULT_POLL_WINDOW_END,
    DEFAULT_POLL_WINDOW_START,
    DEFAULT_PORT,
    DEFAULT_SCAN_WINDOW_DAYS,
    DEFAULT_USE_SSL,
    DOMAIN,
    OPT_ARCHIVE_FOLDER,
    OPT_DELETE_AFTER_DAYS,
    OPT_EMAIL_CLEANUP,
    OPT_POLL_INTERVAL,
    OPT_POLL_TIMEZONE,
    OPT_POLL_WINDOW_END,
    OPT_POLL_WINDOW_START,
    OPT_SCAN_WINDOW_DAYS,
)
from .coordinator import discover_eics
from .imap_client import (
    ImapAuthError,
    ImapClient,
    ImapConnectionError,
    ImapFolderError,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_USE_SSL, default=DEFAULT_USE_SSL): bool,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


def _unique_id(host: str, port: int, username: str) -> str:
    return f"{username}@{host}:{port}"


class OkteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._user_data: dict[str, Any] = {}
        self._folders: list[str] = []
        self._discovered: list[tuple[str, str]] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None

    # ----- user step ---------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            unique = _unique_id(
                user_input[CONF_HOST],
                user_input[CONF_PORT],
                user_input[CONF_USERNAME],
            )
            await self.async_set_unique_id(unique)
            self._abort_if_unique_id_configured()

            client = ImapClient(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                folder=DEFAULT_FOLDER,
                use_ssl=user_input[CONF_USE_SSL],
            )
            try:
                folders = await self.hass.async_add_executor_job(
                    client.list_folders
                )
            except ImapAuthError:
                errors["base"] = "invalid_auth"
            except ImapConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                self._user_data = user_input
                self._folders = folders or [DEFAULT_FOLDER]
                return await self.async_step_folder()

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    async def async_step_folder(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        # Pre-select INBOX if present, else the first folder.
        default = (
            DEFAULT_FOLDER if DEFAULT_FOLDER in self._folders else self._folders[0]
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_FOLDER, default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=f, label=f)
                            for f in self._folders
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        if user_input is None:
            return self.async_show_form(
                step_id="folder", data_schema=schema, errors=errors
            )

        try:
            discovered = await self.hass.async_add_executor_job(
                discover_eics,
                self._user_data[CONF_HOST],
                self._user_data[CONF_PORT],
                self._user_data[CONF_USERNAME],
                self._user_data[CONF_PASSWORD],
                user_input[CONF_FOLDER],
                self._user_data[CONF_USE_SSL],
            )
        except ImapAuthError:
            errors["base"] = "invalid_auth"
        except ImapFolderError:
            errors[CONF_FOLDER] = "folder_not_found"
        except ImapConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during config flow discovery")
            errors["base"] = "unknown"
        else:
            self._user_data = {**self._user_data, CONF_FOLDER: user_input[CONF_FOLDER]}
            self._discovered = discovered
            if not discovered:
                errors["base"] = "no_eics_found"
            else:
                return await self.async_step_discover_eics()

        return self.async_show_form(
            step_id="folder", data_schema=schema, errors=errors
        )

    # ----- discover_eics step ------------------------------------------

    async def async_step_discover_eics(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        options = [
            selector.SelectOptionDict(
                value=eic, label=f"{eic} ({role})"
            )
            for eic, role in self._discovered
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_EICS,
                    default=[eic for eic, _ in self._discovered],
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        if user_input is None:
            return self.async_show_form(
                step_id="discover_eics", data_schema=schema
            )

        selected = set(user_input[CONF_EICS])
        eic_records = [
            {"eic": eic, "role": role, "enabled": eic in selected}
            for eic, role in self._discovered
        ]
        entry_data = {**self._user_data, CONF_EICS: eic_records}
        return self.async_create_entry(
            title=self._user_data[CONF_USERNAME],
            data=entry_data,
        )

    # ----- reauth ------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        assert self._reauth_entry is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            client = ImapClient(
                host=self._reauth_entry.data[CONF_HOST],
                port=self._reauth_entry.data[CONF_PORT],
                username=self._reauth_entry.data[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                folder=self._reauth_entry.data[CONF_FOLDER],
                use_ssl=self._reauth_entry.data[CONF_USE_SSL],
            )
            try:
                await self.hass.async_add_executor_job(client.verify_credentials)
            except ImapAuthError:
                errors["base"] = "invalid_auth"
            except ImapConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
        )

    # ----- options flow ------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return OkteOptionsFlow(config_entry)


class OkteOptionsFlow(config_entries.OptionsFlow):
    """Options flow.

    Supports two paths from a menu:
    - "options": tweak polling and cleanup parameters, and per-EIC enable
      toggles.
    - "rescan": re-run mailbox discovery to pick up newly added EICs.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._discovered: list[tuple[str, str]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["options", "rescan"],
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        existing_eics = self.config_entry.data.get(CONF_EICS, [])
        opts = self.config_entry.options

        # Build the dynamic schema with one toggle per EIC.
        schema_dict: dict[Any, Any] = {
            vol.Required(
                OPT_POLL_INTERVAL,
                default=opts.get(OPT_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): vol.All(int, vol.Range(min=5, max=1440)),
            vol.Required(
                OPT_POLL_WINDOW_START,
                default=opts.get(
                    OPT_POLL_WINDOW_START, DEFAULT_POLL_WINDOW_START
                ),
            ): selector.TimeSelector(),
            vol.Required(
                OPT_POLL_WINDOW_END,
                default=opts.get(
                    OPT_POLL_WINDOW_END, DEFAULT_POLL_WINDOW_END
                ),
            ): selector.TimeSelector(),
            vol.Required(
                OPT_POLL_TIMEZONE,
                default=opts.get(OPT_POLL_TIMEZONE, DEFAULT_POLL_TIMEZONE),
            ): str,
            vol.Required(
                OPT_EMAIL_CLEANUP,
                default=opts.get(OPT_EMAIL_CLEANUP, DEFAULT_EMAIL_CLEANUP),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[CLEANUP_LEAVE, CLEANUP_ARCHIVE, CLEANUP_DELETE],
                    translation_key="email_cleanup",
                )
            ),
            vol.Optional(
                OPT_ARCHIVE_FOLDER,
                default=opts.get(OPT_ARCHIVE_FOLDER, DEFAULT_ARCHIVE_FOLDER),
            ): str,
            vol.Optional(
                OPT_DELETE_AFTER_DAYS,
                default=opts.get(
                    OPT_DELETE_AFTER_DAYS, DEFAULT_DELETE_AFTER_DAYS
                ),
            ): vol.All(int, vol.Range(min=1, max=3650)),
            vol.Optional(
                OPT_SCAN_WINDOW_DAYS,
                default=opts.get(
                    OPT_SCAN_WINDOW_DAYS, DEFAULT_SCAN_WINDOW_DAYS
                ),
            ): vol.All(int, vol.Range(min=1, max=3650)),
        }
        for record in existing_eics:
            eic = record["eic"]
            schema_dict[
                vol.Required(
                    f"enable_{eic}", default=record.get("enabled", True)
                )
            ] = bool

        if user_input is None:
            return self.async_show_form(
                step_id="options", data_schema=vol.Schema(schema_dict)
            )

        # Split EIC toggles from generic options.
        eic_toggles = {
            k[len("enable_"):]: v
            for k, v in user_input.items()
            if k.startswith("enable_")
        }
        plain_options = {
            k: v for k, v in user_input.items() if not k.startswith("enable_")
        }

        # Apply per-EIC enables to entry.data (not entry.options) so they
        # round-trip through the same place the user originally chose them.
        new_eics = [
            {
                **record,
                "enabled": eic_toggles.get(record["eic"], record.get("enabled", True)),
            }
            for record in existing_eics
        ]
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_EICS: new_eics},
        )
        return self.async_create_entry(title="", data=plain_options)

    async def async_step_rescan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            try:
                self._discovered = await self.hass.async_add_executor_job(
                    discover_eics,
                    self.config_entry.data[CONF_HOST],
                    self.config_entry.data[CONF_PORT],
                    self.config_entry.data[CONF_USERNAME],
                    self.config_entry.data[CONF_PASSWORD],
                    self.config_entry.data[CONF_FOLDER],
                    self.config_entry.data[CONF_USE_SSL],
                )
            except ImapAuthError:
                return self.async_abort(reason="invalid_auth")
            except (ImapConnectionError, ImapFolderError):
                return self.async_abort(reason="cannot_connect")
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during rescan")
                return self.async_abort(reason="unknown")
            if not self._discovered:
                return self.async_abort(reason="no_eics_found")

        existing_eics = {
            r["eic"]: r for r in self.config_entry.data.get(CONF_EICS, [])
        }
        defaults = [
            eic for eic in existing_eics if existing_eics[eic].get("enabled", True)
        ] + [eic for eic, _ in self._discovered if eic not in existing_eics]
        options = [
            selector.SelectOptionDict(value=eic, label=f"{eic} ({role})")
            for eic, role in self._discovered
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_EICS, default=defaults): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        if user_input is None:
            return self.async_show_form(
                step_id="rescan", data_schema=schema, errors=errors
            )

        selected = set(user_input[CONF_EICS])
        merged: dict[str, dict[str, Any]] = {}
        for eic, role in self._discovered:
            merged[eic] = {
                "eic": eic,
                "role": role,
                "enabled": eic in selected,
            }
        # Keep previously-known EICs that didn't show up in this rescan
        # but disable them (user didn't include them in the new selection).
        for eic, record in existing_eics.items():
            merged.setdefault(
                eic,
                {**record, "enabled": eic in selected},
            )
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_EICS: list(merged.values()),
            },
        )
        return self.async_create_entry(title="", data=self.config_entry.options)
