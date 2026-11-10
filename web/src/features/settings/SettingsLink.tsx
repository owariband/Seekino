import { Link, type LinkProps, useLocation } from "react-router";

export function SettingsLink({ section, ...props }: Omit<LinkProps, "to" | "state"> & { section?: "models" | "model-roles" }) {
  const location = useLocation();
  return <Link {...props} to={section ? `/settings?section=${section}` : "/settings"}
    state={location.pathname === "/settings" ? location.state : { settingsFrom: location.key }} />;
}
