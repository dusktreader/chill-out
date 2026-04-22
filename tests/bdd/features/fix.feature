Feature: chill-out check --fix
  As a developer
  I want chill-out to rewrite my manifest with safe pins
  So that I can clear a cooldown violation without hand-editing

  Background:
    Given a fresh pypi project at the working directory

  Scenario: --fix pins a violating dep at the safe version
    Given the project depends on "fresh" pinned at "2.0.0"
    And pypi reports "fresh 2.0.0" was published 1 days ago
    And pypi reports "fresh 1.5.0" was published 200 days ago
    When I run "chill-out check --quiet --fix --no-recheck"
    Then the manifest contains "fresh==1.5.0"

  Scenario: --fix with compatible style writes a range
    Given the project depends on "fresh" pinned at "2.0.0"
    And pypi reports "fresh 2.0.0" was published 1 days ago
    And pypi reports "fresh 1.5.0" was published 200 days ago
    When I run "chill-out check --quiet --fix --no-recheck --fix-style compatible"
    Then the manifest contains "fresh>=1.5.0,<2.0.0"

  Scenario: --fix preserves an existing lower bound when using compatible style
    Given the project depends on "fresh" with spec "fresh>=1.4"
    And the lockfile resolves "fresh" to "2.0.0"
    And pypi reports "fresh 2.0.0" was published 1 days ago
    And pypi reports "fresh 1.5.0" was published 200 days ago
    When I run "chill-out check --quiet --fix --no-recheck --fix-style compatible"
    Then the manifest contains "fresh>=1.4,<2.0.0"
