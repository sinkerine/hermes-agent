// @vitest-environment happy-dom
/* Runs only in the sandbox archive of pinned Element, against real Synapse dumps. */
import { MatrixEvent, KnownMembership, type MatrixClient, type Room } from "matrix-js-sdk/src/matrix";
import { afterEach, describe, expect, it, vi } from "vitest";

import dumps from "./hermes-publication-dumps.json";
import { parseBotCommandDescription, BOT_COMMAND_DESCRIPTION_EVENT_TYPE } from "./BotCommandProtocol";
import { getRoomBotCommands, resolveBotCommand } from "./RoomBotCommands";
import { createBotCommandContent } from "./BotCommandContent";
import SettingsStore from "../../settings/SettingsStore";
import { CommandMap } from "../SlashCommands";

const bot = "@hermes:localhost";
afterEach(() => vi.restoreAllMocks());

function setup(raw: unknown[]) {
    const events = raw.map((ev) => new MatrixEvent(ev as ConstructorParameters<typeof MatrixEvent>[0]));
    const members = new Map(events.filter((ev) => ev.getType() === "m.room.member")
        .map((ev) => [ev.getStateKey(), { membership: ev.getContent().membership,
            name: ev.getContent().displayname || ev.getStateKey() }]));
    const room = {
        roomId: "!conformance:localhost",
        getLiveTimeline: () => ({ getState: () => ({ getStateEvents: (type: string) =>
            events.filter((ev) => ev.getType() === type) }) }),
        getMember: (id: string) => members.get(id),
    } as unknown as Room;
    const client = { getUserId: () => "@alice:localhost" } as MatrixClient;
    return { events, room, client };
}

describe("Hermes actual publication conformance", () => {
    for (const [label, raw] of Object.entries(dumps)) {
        it(`${label}: actual state parses and resolves through pinned Element`, () => {
            vi.spyOn(SettingsStore, "getValue").mockImplementation((name: string) => name === "feature_user_status");
            const { events, room, client } = setup(raw);
            const descriptions = events.filter((ev) => ev.getType() === BOT_COMMAND_DESCRIPTION_EVENT_TYPE && ev.getSender() === bot);
            expect(descriptions.length).toBeGreaterThan(0);
            expect(room.getMember(bot)?.membership).toBe(KnownMembership.Join);
            for (const ev of descriptions) {
                if (Object.keys(ev.getContent()).length === 0) expect(parseBotCommandDescription(ev)).toBeNull();
                else expect(parseBotCommandDescription(ev)).not.toBeNull();
            }
            if (label === "disabled" || label === "ops-disabled") {
                expect(descriptions.every((ev) => Object.keys(ev.getContent()).length === 0)).toBe(true);
                expect(getRoomBotCommands(room, client)).toEqual([]);
                expect(resolveBotCommand(room, client, "/status@hermes")).toBeNull();
                return;
            }
            // Use the real enabled built-in, rather than replacing CommandMap with a fixture.
            expect(CommandMap.get("status")?.isEnabled(client, room.roomId)).toBe(true);
            const commands = getRoomBotCommands(room, client);
            expect(commands.find((command) => command.command === "status")).toMatchObject({
                botUserId: bot, qualifier: "hermes", requiresQualifier: true,
            });
            expect(resolveBotCommand(room, client, "/status")).toBeNull();
            const resolved = resolveBotCommand(room, client, "/status@hermes");
            expect(resolved).not.toBeNull();
            expect(createBotCommandContent(resolved!)).toMatchObject({
                "body": "/status", "m.mentions": { user_ids: [bot] },
                "org.matrix.msc4391.command": { command: "status", arguments: {} },
            });
            if (label === "dm-removed") {
                expect(commands.some((command) => command.command === "mcd-probe")).toBe(false);
                expect(descriptions.some((ev) => Object.keys(ev.getContent()).length === 0)).toBe(true);
            }
        });
    }
});
