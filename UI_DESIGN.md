# Pullarr UI design preferences

Saved from the user’s redesign request on 2026-10-28. Keep these preferences for future UI work.

- Keep a dark theme. Palette: https://coolors.co/ec4e20-ff9505-016fb9-64113f-111d13
- Exact colors: red `#EC4E20`, orange `#FF9505`, blue `#016FB9`, plum `#64113F`, deep green `#111D13`.
- Use deep green for dark surfaces, orange for primary actions, blue for selected navigation and enabled toggles, and red/plum as supporting accents. Use lighter text variants for readable contrast.
- Left navigation: Home, Channels, Downloads, Settings. Home provides an overview.
- Reference: the user-provided dashboard image, with a narrow sidebar, restrained borders, small controls, summary cards, and dense tables.
- Channels and Downloads use compact tables; settings use the same compact spacing and aligned rows. Keep mobile navigation accessible and allow tables to scroll horizontally.
- Preserve safe rendering, fixed-size toggles, confirmations for removal, live progress, and existing subscription/profile/settings behavior.

- Show a single small version number beside the Pullarr logo text. Omit the downloader/version sidebar footer and repeated top-bar branding.
- Home activity and Queue & downloads use the same Video, Channel, and Status columns. Keep progress and removal controls in the Status column.
- Downloads progress bars are 28px high, with white status/percentage text centered inside a blue-filled bar (for example, Downloading 30%). Keep native progress accessibility and unknown-percentage states.
