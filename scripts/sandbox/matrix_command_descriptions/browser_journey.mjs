// Real pinned Element UI. Never imports fixture command descriptions.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const [step, sb] = process.argv.slice(2);
assert(sb && path.isAbsolute(sb));
const read = async (name) => JSON.parse(await fs.readFile(path.join(sb, name), 'utf8'));
const save = async (name, value) => fs.writeFile(path.join(sb, 'evidence', name), JSON.stringify(value, null, 2), { mode: 0o600 });
const accounts = await read('accounts.json');
const packages = await fs.readdir(path.join(sb, 'element-src/node_modules/.pnpm'));
const pkg = packages.find((name) => /^playwright@/.test(name));
assert(pkg, 'Pinned Element Playwright installation missing');
const { chromium } = await import(pathToFileURL(path.join(sb, 'element-src/node_modules/.pnpm', pkg, 'node_modules/playwright/index.mjs')));
const chrome = '/nix/store/dqzn64123p2qvidnx0wspr0z96rc407a-google-chrome-156.0.8078.79/bin/google-chrome-stable';
// Chrome's singleton/socket paths have a small length limit. Keep TMPDIR relative to cwd.
process.chdir(path.join(sb, 'tmp'));
await fs.mkdir('chrome-tmp', { recursive: true, mode: 0o700 });
const context = await chromium.launchPersistentContext(path.join(sb, 'browser/alice'), {
    executablePath: chrome, chromiumSandbox: true, headless: true,
    viewport: { width: 1440, height: 1100 },
    env: { ...process.env, TMPDIR: 'chrome-tmp', TMP: 'chrome-tmp', TEMP: 'chrome-tmp' },
});
const page = context.pages()[0] || await context.newPage();
page.setDefaultTimeout(30000);
const eventType = 'org.matrix.msc4391.command_description';
const bot = '@hermes:localhost';
const clientReady = async () => page.waitForFunction(() => window.mxMatrixClientPeg?.get()?.getSyncState() === 'SYNCING', null, { timeout: 90000 });
const screenshot = async (name) => page.screenshot({ path: path.join(sb, 'evidence', name), fullPage: true });
const composer = () => page.locator('[contenteditable="true"][role="textbox"], .mx_SendMessageComposer [contenteditable="true"]').last();
async function type(text) {
    await composer().fill(text);
}
async function room(name) {
    const rooms = await read('rooms.json');
    assert(rooms[name], `Missing room ${name}`);
    await page.goto('http://127.0.0.1:18181/#/room/' + encodeURIComponent(rooms[name]));
    await clientReady();
    await composer().waitFor();
    return rooms[name];
}
async function commands(roomId, present, command = 'status') {
    await page.waitForFunction(({ roomId, present, command, eventType, bot }) => {
        const room = window.mxMatrixClientPeg.get().getRoom(roomId);
        const events = room?.currentState.getStateEvents(eventType) || [];
        const found = events.some((ev) => ev.getSender() === bot && ev.getContent().command === command);
        return found === present;
    }, { roomId, present, command, eventType, bot }, { timeout: 120000 });
}
async function noModel() {
    const counts = await (await fetch('http://127.0.0.1:18182/counts')).json();
    assert.equal(counts.completions, 0);
}
async function botRows(present) {
    const rows = page.locator('.mx_Autocomplete').getByText(/\(from hermes\)/);
    if (present) await rows.first().waitFor();
    else {
        await page.locator('.mx_Autocomplete').waitFor();
        assert.equal(await rows.count(), 0);
    }
}
async function reload(present) {
    const id = await room('dm');
    const before = await page.evaluate((roomId) => window.mxMatrixClientPeg.get().getRoom(roomId)
        .getLiveTimeline().getEvents().map((ev) => ev.getId()), id);
    await type('/reload-skills');
    await composer().press('Enter');
    await page.waitForFunction(({ id, before, bot }) => window.mxMatrixClientPeg.get().getRoom(id)
        .getLiveTimeline().getEvents().some((ev) => ev.getSender() === bot && !before.includes(ev.getId()) &&
            /reload|skill/i.test(ev.getContent().body || '')), { id, before, bot }, { timeout: 30000 });
    await commands(id, present, 'mcd-probe');
    await type('/mcd');
    const probe = page.locator('.mx_Autocomplete').getByText('/mcd-probe', { exact: true });
    if (present) await probe.waitFor();
    else assert.equal(await probe.count(), 0);
    await noModel();
}

try {
    if (step === 'login-and-dm') {
        await page.goto('http://127.0.0.1:18181/#/login');
        await page.getByRole('textbox', { name: /username|email|phone/i }).fill('alice');
        await page.locator('input[type="password"]').fill(accounts.alice.password);
        await page.getByRole('button', { name: /^sign in$/i }).click();
        // Dismiss only optional post-login setup; encryption remains enabled.
        const skip = page.getByRole('button', { name: /skip.*now|skip.*later|skip$/i });
        if (await skip.isVisible()) await skip.click();
        await clientReady();
        const id = await page.evaluate(async (bot) => {
            const cli = window.mxMatrixClientPeg.get();
            const result = await cli.createRoom({ is_direct: true, invite: [bot], preset: 'trusted_private_chat',
                initial_state: [{ type: 'm.room.encryption', state_key: '', content: { algorithm: 'm.megolm.v1.aes-sha2' } }] });
            const direct = cli.getAccountData('m.direct')?.getContent() || {};
            await cli.setAccountData('m.direct', { ...direct, [bot]: [...(direct[bot] || []), result.room_id] });
            return result.room_id;
        }, bot);
        const rooms = await read('rooms.json');
        await fs.writeFile(path.join(sb, 'rooms.json'), JSON.stringify({ ...rooms, dm: id }, null, 2), { mode: 0o600 });
        await room('dm');
        await page.waitForFunction(({ id, bot }) => window.mxMatrixClientPeg.get().getRoom(id)?.getMember(bot)?.membership === 'join', { id, bot });
        assert.equal(await page.evaluate((id) => window.mxMatrixClientPeg.get().getRoom(id).currentState
            .getStateEvents('m.room.encryption', '').getContent().algorithm, id), 'm.megolm.v1.aes-sha2');
        await screenshot('01-dm.png');
    } else if (step === 'autocomplete-status') {
        const id = await room('dm');
        await commands(id, true);
        await type('/sta');
        await page.locator('.mx_Autocomplete').getByText('/status@hermes', { exact: true }).waitFor();
        await page.locator('.mx_Autocomplete').getByText('/status', { exact: true }).waitFor();
        await botRows(true);
        await screenshot('02-autocomplete-status.png');
    } else if (step === 'run-status') {
        const id = await room('dm');
        await type('/sta');
        await page.locator('.mx_Autocomplete').getByText('/status@hermes', { exact: true }).click();
        await composer().press('Enter');
        await page.waitForFunction(({ id, bot }) => window.mxMatrixClientPeg.get().getRoom(id).getLiveTimeline()
            .getEvents().some((ev) => ev.getSender() === bot && /Hermes Gateway Status/.test(ev.getContent().body || '')), { id, bot }, { timeout: 90000 });
        const sent = await page.evaluate((id) => {
            const cli = window.mxMatrixClientPeg.get();
            const events = cli.getRoom(id).getLiveTimeline().getEvents();
            return events.filter((ev) => ev.getSender() === cli.getUserId() && ev.getContent().body === '/status').at(-1)?.getContent();
        }, id);
        assert.equal(sent?.body, '/status');
        assert.deepEqual(sent['m.mentions'].user_ids, [bot]);
        assert.equal(sent['org.matrix.msc4391.command'].command, 'status');
        await save('sent-status.json', sent);
        await page.getByText(/Hermes Gateway Status/).last().waitFor();
        const body = await page.locator('body').innerText();
        assert(body.includes(id), 'Gateway status reply must include DM room id');
        await screenshot('03-status-reply.png');
        const before = await page.evaluate((id) => window.mxMatrixClientPeg.get().getRoom(id).getLiveTimeline().getEvents()
            .filter((ev) => ev.getContent().body === '/status').length, id);
        await type('/status');
        await composer().press('Escape');
        await composer().press('Enter');
        await page.getByRole('dialog').waitFor();
        assert.match(await page.getByRole('dialog').innerText(), /status|argument|emoji/i);
        assert.equal(await page.evaluate((id) => window.mxMatrixClientPeg.get().getRoom(id).getLiveTimeline().getEvents()
            .filter((ev) => ev.getContent().body === '/status').length, id), before);
        await screenshot('04-builtin-status.png');
        await noModel();
    } else if (step === 'ops-empty' || step === 'ops-granted') {
        const id = await room('ops');
        const present = step === 'ops-granted';
        await commands(id, present);
        await type('/');
        await botRows(present);
        await screenshot(present ? '06-ops-granted.png' : '05-ops-no-power.png');
    } else if (step === 'reload-added') {
        await reload(true);
        await screenshot('07-reload-added.png');
    } else if (step === 'reload-removed') {
        await reload(false);
        await screenshot('07-reload-removed.png');
    } else if (step === 'disabled') {
        const id = await room('dm');
        await commands(id, false);
        const rooms = await read('rooms.json');
        await page.waitForFunction(({ ids, eventType, bot }) => ids.every((id) => {
            const events = window.mxMatrixClientPeg.get().getRoom(id)?.currentState.getStateEvents(eventType) || [];
            const published = events.filter((ev) => ev.getSender() === bot);
            return published.length > 0 && published.every((ev) => Object.keys(ev.getContent()).length === 0);
        }), { ids: [id, rooms.ops], eventType, bot }, { timeout: 120000 });
        await type('/');
        await botRows(false);
        await type('/status@hermes');
        await composer().press('Escape');
        await composer().press('Enter');
        await page.getByRole('dialog').waitFor();
        assert.match(await page.getByRole('dialog').innerText(), /unknown|unrecognised|unrecognized/i);
        await screenshot('08-disabled.png');
        await noModel();
    } else throw new Error('Unknown browser step');
    await save(`browser-${step}.json`, { step, passed: true });
} finally {
    await context.close();
}
