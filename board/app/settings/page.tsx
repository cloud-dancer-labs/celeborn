import BoardHeader from "../BoardHeader";
import SettingsView from "./SettingsView";

// Always render fresh; the page reflects live ~/.claude + project settings and the skills on disk.
export const dynamic = "force-dynamic";
export const metadata = { title: "Celeborn — Settings" };

export default function SettingsPage() {
  return (
    <main className="board-page">
      <BoardHeader
        active="settings"
        subtitle="Settings · skills, auto-allows & the Danger Zone"
      />
      <SettingsView />
    </main>
  );
}
