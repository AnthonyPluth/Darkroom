import UI from "./app/ui.js";
import * as Log from "./core/util/logging.js";

async function settings(name) {
    try {
        const response = await fetch(`./${name}.json`);
        if (!response.ok) {
            throw Error(`${response.status} ${response.statusText}`);
        }
        return await response.json();
    } catch (err) {
        Log.Error(`Couldn't fetch ${name}.json: ${err}`);
        return {};
    }
}

UI.start({ settings: { defaults: await settings("defaults"), mandatory: await settings("mandatory") } });
