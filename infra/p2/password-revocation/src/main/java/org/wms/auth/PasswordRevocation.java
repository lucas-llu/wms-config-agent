package org.wms.auth;

import org.keycloak.events.Event;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventType;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.models.KeycloakSession;

/** Revoke old online/offline sessions server-side, independent of form checkboxes. */
public final class PasswordRevocation implements EventListenerProvider {
    private final KeycloakSession session;
    public PasswordRevocation(KeycloakSession session) { this.session = session; }
    @Override public void onEvent(Event event) {
        boolean password = event.getType() == EventType.UPDATE_PASSWORD ||
            (event.getType() == EventType.UPDATE_CREDENTIAL && event.getDetails() != null &&
             "password".equals(event.getDetails().get("credential_type")));
        if (!password || event.getUserId() == null || event.getRealmId() == null) return;
        var realm = session.realms().getRealm(event.getRealmId());
        if (realm == null) return;
        var user = session.users().getUserById(realm, event.getUserId());
        if (user == null) return;
        session.sessions().getOfflineUserSessionsStream(realm, user).toList()
            .forEach(s -> session.sessions().removeOfflineUserSession(realm, s));
        session.sessions().removeUserSessions(realm, user);
    }
    @Override public void onEvent(AdminEvent event, boolean includeRepresentation) { }
    @Override public void close() { }
}
