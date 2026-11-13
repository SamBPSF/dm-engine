# Starting a new campaign

The player wants a new game. Get them from that sentence to "Prep complete" with as few questions as
possible. The lists returned with this guide show the settings, characters and table rules already saved.

1. **Work out what you still need.** Anything the player already said, or that is on the lists, you do
   not ask again. In ONE message, ask only for what is missing:
   - **Setting.** A saved setting by name, a new one described in a few sentences, a published setting
     they own books for, or "invent one" with the feel they want (genre, tone).
   - **Character.** A character from the library by name, or a new one. For a new one, ask them to
     attach or paste the character sheet.
   - **Length.** A short adventure of two or three sessions, or something longer. If they do not say,
     assume short.
   If there is exactly one saved character and they did not mention another, assume that character.

2. **New character only:** save the full sheet to the library with library_save before anything else.
   Copy everything on the sheet, in these shapes:
   - `max_hp`, `ac`, `init` (bonus), `speed`, `level`, `class`, `species`, `background`, `proficiency_bonus`
   - `abilities` {str, dex, con, int, wis, cha: score}, `saves` {the same six: bonus}, `skills` {name: bonus}
   - `attacks` [{name, to_hit, damage such as "1d8+3", type, count if it is made more than once, rider,
     once_per_turn for extra dice added to one hit a turn, such as "2d6" for Sneak Attack}]: every
     weapon and every attack cantrip
   - `spellcasting` {ability, save_dc, attack_bonus}, `max_slots` {"1": 4, "2": 2}, `spells` {"1": [names]},
     `cantrips` [names]
   - `resources` {name: {max, recharge: "short" or "long"}} for anything with limited uses
   - `features` [one line each: name, then what it does], `inventory`, `money`, `proficiencies`
   - `notes`: who they are, in a few lines
   The reply lists anything the engine still needs (`sheet_is_missing`) and the reactions it recognised.
   If something is missing that the sheet does not show, ask the player once, in one message. If their
   notes are thin, ask for three things: what the character wants, someone they care about, and someone
   they owe or fear. The Author builds the hook from those.

3. **Name the campaign yourself.** A short lowercase name: the setting, plus a number if that name is
   taken (the list of existing campaigns is below). Tell the player the name; it is how they will come
   back to this game.

4. **Call create_campaign.** Pass the setting name. Pass a description and tone only for a new or
   invented setting; a saved setting is copied automatically. Pass the character name to copy that
   character from the library, fully rested: this works for a character you saved a moment ago too. Leave table_rules empty to reuse the player's saved rules.

   **If the player has books or long notes for the setting** and create_campaign reports none on the
   shelf, call lore_upload_link with the setting name, give them the link, and wait until they say the
   books are in. Check with lore_shelf. Then carry on. Do not ask them to paste a book into the chat.

5. **Run the Author.** Call author_brief for the new campaign and follow its procedure. That work is
   sealed: reveal none of it.

6. **Finish** with: "Prep complete. Your campaign is called <name>. To play, say: Start a session in
   <name>." Add nothing about the content.
