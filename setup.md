# Starting a new campaign

The player wants a new game. Get them from that sentence to "Prep complete" with as few questions as
possible. The lists returned with this guide show the settings, characters and table rules already saved.

1. **Work out what you still need.** Anything the player already said, or that is on the lists, you do
   not ask again. In ONE message, ask only for what is missing:
   - **Setting.** A saved setting by name, a new one described in a few sentences, or "invent one" with
     the feel they want (genre, tone).
   - **Character.** A saved character by name, or a new one. For a new one, ask them to attach or paste
     the character sheet.
   - **Length.** A short adventure of two or three sessions, or something longer. If they do not say,
     assume short.
   If there is exactly one saved character and they did not mention another, assume that character.

2. **Name the campaign yourself.** A short lowercase name: the setting, plus a number if that name is
   taken (the list of existing campaigns is below). Tell the player the name; it is how they will come
   back to this game.

3. **Call create_campaign.** Pass the setting name. Pass a description and tone only for a new or
   invented setting; a saved setting is copied automatically. Pass the character name to copy a saved
   character, fully rested. Leave table_rules empty to reuse the player's saved rules.

4. **New character only:** save the full sheet with save_character (abilities, saves, skills, spells,
   slots, resources, features, inventory) before going on.

5. **Run the Author.** Call author_brief for the new campaign and follow its procedure. That work is
   sealed: reveal none of it.

6. **Finish** with: "Prep complete. Your campaign is called <name>. To play, say: Start a session in
   <name>." Add nothing about the content.
