Feature: chill-out check
  As a developer
  I want chill-out to surface dependencies that are still inside their cooldown window
  So that I don't ship a freshly-released (and possibly compromised) package

  Background:
    Given a fresh pypi project at the working directory

  Scenario: Every dependency is well past its cooldown
    Given the project depends on "settled" pinned at "1.0.0"
    And pypi reports "settled 1.0.0" was published 400 days ago
    When I run "chill-out check --quiet"
    Then the command exits cleanly
    And the output contains "No cooldown violations"

  Scenario: A dependency is still inside its cooldown window
    Given the project depends on "fresh" pinned at "2.0.0"
    And pypi reports "fresh 2.0.0" was published 1 days ago
    And pypi reports "fresh 1.5.0" was published 200 days ago
    When I run "chill-out check --quiet"
    Then the command exits with a violation
    And the output mentions "fresh"
    And the output mentions "1.5.0"

  Scenario: Mixed project with both fresh and settled deps
    Given the project depends on "fresh" pinned at "2.0.0"
    And pypi reports "fresh 2.0.0" was published 1 days ago
    And pypi reports "fresh 1.5.0" was published 200 days ago
    And the project depends on "settled" pinned at "1.0.0"
    And pypi reports "settled 1.0.0" was published 400 days ago
    When I run "chill-out check --quiet"
    Then the command exits with a violation
    And the output mentions "fresh"
    And the output does not mention "settled" in the violation table

  Scenario: Fast mode skips the safe-version lookup
    Given the project depends on "fresh" pinned at "2.0.0"
    And pypi reports "fresh 2.0.0" was published 1 days ago
    When I run "chill-out check --quiet --fast"
    Then the command exits with a violation
    And the output mentions "fresh"
