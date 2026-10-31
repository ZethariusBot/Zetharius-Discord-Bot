# Zetharius Discord Bot — Privacy Policy

**Last updated: 31 October 2026**

> **Before publishing:** Replace every bracketed placeholder with accurate details. In particular, identify the real person or organisation operating the Bot and provide a monitored contact email. Confirm that the listed features and data practices still match the deployed version.

## 1. Who operates the Bot

The Zetharius Discord bot (the **“Bot”**) is operated by **[YOUR FULL LEGAL NAME OR ORGANISATION]**, located in **[COUNTRY]**. For privacy questions or requests, contact **[CONTACT EMAIL]**. This operator is the controller for personal data processed through Bot features under the operator’s control.

## 2. Data the Bot processes

Depending on which features a Discord server and its users choose to use, the Bot may process:

- **Discord identifiers and server information:** Discord user, server, channel, role, thread and message IDs; usernames or display names where needed to show a result; and server membership or role information used to check permissions and sign-ups.
- **Messages used for the levels feature:** The Bot receives message events in servers where it is installed and has the required Discord permissions. It checks message length and a cooldown in memory to award experience. It does not save ordinary message text for this feature. It saves the user and server IDs with the user’s experience points and level.
- **Albion Online content and settlement data:** Sign-ups, role selections, organizer-entered participant details, event details, loot or silver amounts, adjustments, approval and payout records, and relevant user IDs. Organizers may enter information about other participants when arranging a content event.
- **Albion profile lookup history:** When a user requests a player profile, the Bot may save the Albion player ID, guild name and guild ID, with timestamps, to show changes in guild membership over time.
- **Albion killboard and server-status data:** The Bot stores event IDs to avoid posting the same kill or death more than once, and channel/message IDs to update its status panel.
- **World of Tanks lookups:** A player name or other search term supplied to a command is sent to Wargaming’s public World of Tanks API to retrieve the requested public game information. The Bot uses its configured Wargaming application ID; it does not ask users for their Wargaming password.
- **Music requests:** A search phrase or URL submitted to the music feature may be sent to YouTube or the relevant media provider through yt-dlp to find and stream the requested audio. The Bot is configured not to download a permanent copy of the audio. Discord voice connections are used to play it.
- **Command and delivery information:** The Bot processes command inputs and the IDs needed to reply, check permissions, create or update panels, and send event or payout notifications. A settlement administrator may use the Bot to send users a direct message stating an amount, collection time and location.

The Bot does not intentionally collect Discord passwords, payment card details, or real-world financial account credentials. “Silver” balances are records of in-game Albion Online amounts; the Bot does not hold or transfer real money or in-game currency.

## 3. Why and on what basis data is processed

The operator processes the data above to provide the Bot’s requested features, maintain sign-ups and settlement records, prevent duplicate event posts, enforce permissions, and keep the Bot reliable. Depending on the feature and context, the operator’s intended legal basis is **[CONFIRM AND SELECT THE APPLICABLE GDPR BASIS, SUCH AS LEGITIMATE INTERESTS IN PROVIDING AND SECURING THE COMMUNITY BOT, OR PERFORMANCE OF A USER-REQUESTED SERVICE]**. Where consent is relied on, it may be withdrawn at any time; withdrawal does not affect prior lawful processing.

Do not publish this policy until the operator has confirmed the applicable legal basis and any legitimate interests being pursued.

## 4. Where data is stored and who receives it

Bot records are stored in local JSON files in the Bot’s hosting environment, currently **ChunkServe**. Discord receives and processes data when the Bot communicates through Discord. When a user invokes a feature that relies on an external game or media service, the minimum relevant search or lookup information may be sent to that service, such as Albion Online game information/status or market services, Wargaming, or YouTube/media providers. Those providers process information under their own terms and privacy notices.

The operator does not sell Bot data or use it for advertising. Access to stored records should be limited to the Bot operator and authorised administrators who need it to manage the relevant feature. Discord independently processes data under [Discord’s Privacy Policy](https://discord.com/privacy).

## 5. Retention

- Experience points and levels are kept while the Bot’s levels feature is used, unless removed by the operator or following a valid request.
- Active content and settlement records are kept while needed to organise the event and account for its silver. A completed event’s Discord card is removed after 2 hours. Ordinary completed event records are then removed from the Bot’s content store. Guild-settlement records awaiting or documenting settlement may remain in the guild-settlement panel and its records until cleared by an authorised administrator or no longer needed.
- Albion guild-history records are kept to show the history created by profile lookups, until removed by the operator or following a valid request.
- Killboard event IDs and panel IDs are kept while needed to prevent duplicate posts and maintain the related panel; they may be replaced or cleared during maintenance.
- Hosting backups and operational logs, if enabled by the hosting provider or operator, may have separate retention periods: **[DESCRIBE ANY BACKUPS OR LOGS AND THEIR RETENTION, OR REMOVE THIS SENTENCE IF NONE ARE KEPT]**.

The Bot does not currently provide a self-service command that deletes every category of a user’s data. To request access, correction, deletion, or restriction, contact **[CONTACT EMAIL]** and include your Discord user ID, the server, and the relevant feature. The operator may need to verify the request and may retain information where a legal obligation or other applicable legal ground requires it. Data in Discord messages or Discord’s own systems may need to be handled separately under Discord’s processes.

## 6. Your choices and rights

Depending on applicable law, you may have rights to request access, correction, erasure, restriction, portability, or to object to certain processing. Where processing relies on consent, you may withdraw it. You may also complain to your local data protection authority. The right to erasure is subject to the conditions and exceptions in applicable law.

Server administrators can remove the Bot from a server to stop future Bot processing there. Removing the Bot does not automatically erase records already stored by the operator; use the contact above to request deletion.

## 7. Security

The operator uses reasonable measures appropriate to a small community Bot, including restricting access to hosting credentials and Bot data. No internet service or storage system can be guaranteed completely secure. Do not submit sensitive personal information through Bot commands or event sign-ups.

## 8. Children

The Bot is intended for use in Discord communities by people who are permitted to use Discord under Discord’s terms and applicable age laws. The Bot is not designed to collect children’s personal data. If you believe a child has provided personal data through the Bot, contact **[CONTACT EMAIL]**.

## 9. Changes and contact

This policy may be updated when the Bot’s features or data practices change. The current version will be published at **[PUBLIC PRIVACY POLICY URL]**. Material changes should be communicated through an appropriate community channel.

Privacy contact: **[CONTACT EMAIL]**

## 10. Related policies

- [Discord Privacy Policy](https://discord.com/privacy)
- [Discord Developer Terms of Service](https://support-dev.discord.com/hc/en-us/articles/8562894815383-Discord-Developer-Terms-of-Service)
- [EU General Data Protection Regulation (GDPR)](https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng)
