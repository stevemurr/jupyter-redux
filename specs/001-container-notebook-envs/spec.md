# Feature Specification: Container-Based Notebook Environments

**Feature Branch**: `001-container-notebook-envs`
**Created**: 2026-04-10
**Status**: Draft
**Input**: User description: "Build an application that is essentially jupyter notebooks swapping the primitives of kernels with environments. Each notebook is its own container/environment. At the top of the notebook the user should be able to include a cell with commands like 'pip install some-package-name' and that package gets installed in the environment. Containers must have full GPU access. Notebooks must be accessible from a browser just like jupyter notebooks."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Create and Execute a Notebook in an Isolated Container (Priority: P1)

A user opens the application in their browser, creates a new notebook,
and begins writing and executing code cells. Behind the scenes, the
system provisions an isolated container for that notebook. The user
writes Python code in cells, executes them, and sees output rendered
inline — just like Jupyter. Each notebook operates in complete
isolation from every other notebook.

**Why this priority**: This is the foundational capability. Without a
working notebook-in-container experience, no other feature has value.
This delivers the core MVP: a browser-based notebook where code runs
inside an isolated environment rather than a shared kernel.

**Independent Test**: Can be fully tested by creating a notebook,
writing a Python expression (e.g., `2 + 2`), executing the cell, and
verifying the output appears. Delivers a working notebook experience.

**Acceptance Scenarios**:

1. **Given** the application is running, **When** a user navigates to
   the application URL in a browser, **Then** they see a notebook
   management interface where they can create a new notebook.
2. **Given** a user creates a new notebook, **When** the notebook
   opens, **Then** an isolated container is provisioned and the user
   sees an empty notebook with an editable code cell.
3. **Given** a notebook is open, **When** the user types `print("hello")`
   in a code cell and executes it, **Then** `hello` is displayed as
   output below the cell within 2 seconds (excluding container cold
   start).
4. **Given** a user has two notebooks open, **When** they define a
   variable `x = 10` in Notebook A, **Then** that variable is NOT
   accessible from Notebook B, confirming isolation.

---

### User Story 2 - Install Packages via Environment Setup Cells (Priority: P2)

A user adds a setup cell at the top of their notebook containing
package installation commands (e.g., `pip install pandas numpy`).
When executed, those packages are installed into that notebook's
container environment and become available for import in subsequent
code cells. The environment configuration persists so the user does
not need to re-install packages every time they reopen the notebook.

**Why this priority**: Package management is what distinguishes this
application from standard Jupyter. Users need to customize their
environment per-notebook — this is the key differentiator.

**Independent Test**: Can be tested by creating a notebook, adding a
setup cell with `pip install requests`, executing it, then importing
`requests` in a subsequent code cell and verifying it succeeds.

**Acceptance Scenarios**:

1. **Given** an open notebook, **When** the user adds a cell at the
   top containing `pip install pandas` and executes it, **Then** the
   package is installed in the notebook's container and installation
   output is displayed.
2. **Given** `pandas` has been installed via a setup cell, **When**
   the user runs `import pandas; print(pandas.__version__)` in a
   subsequent cell, **Then** the version number is displayed
   successfully.
3. **Given** a notebook with installed packages, **When** the user
   closes and reopens the notebook, **Then** previously installed
   packages remain available without re-execution of the setup cell.
4. **Given** a setup cell with an invalid package name, **When** the
   user executes it, **Then** the system displays a clear error
   message indicating the package was not found.

---

### User Story 3 - GPU-Accelerated Computing (Priority: P3)

A user creates a notebook intended for machine learning or scientific
computing. Their container has full access to the host machine's GPU
resources. The user installs GPU-accelerated libraries (e.g., PyTorch
with CUDA support) via a setup cell and runs GPU computations
directly from notebook cells.

**Why this priority**: GPU access is a stated requirement and a
major value proposition for data science and ML workloads. However,
it builds on top of the container and package installation
capabilities from P1 and P2.

**Independent Test**: Can be tested by creating a notebook, installing
a GPU library, and executing a cell that queries GPU availability
(e.g., `torch.cuda.is_available()`) and runs a simple GPU operation.

**Acceptance Scenarios**:

1. **Given** a notebook running in a container on a GPU-equipped host,
   **When** the user installs PyTorch and runs
   `import torch; print(torch.cuda.is_available())`, **Then** the
   output is `True`.
2. **Given** GPU access is available, **When** the user creates a
   tensor on the GPU and performs a matrix multiplication, **Then**
   the computation completes successfully and results are displayed.
3. **Given** the host has multiple GPUs, **When** a notebook container
   is started, **Then** all available GPUs are accessible from within
   the container.

---

### User Story 4 - Notebook Management and Persistence (Priority: P4)

A user manages multiple notebooks over time. They can see a list of
their existing notebooks, open any previously created notebook, rename
notebooks, and delete notebooks they no longer need. Notebook content
(cells, outputs) and environment state persist across browser sessions
and container restarts.

**Why this priority**: Persistence and management are essential for
real-world usage but are incremental on top of the core notebook
execution experience.

**Independent Test**: Can be tested by creating a notebook, adding
cells with content, closing the browser, reopening it, and verifying
the notebook and its content are intact.

**Acceptance Scenarios**:

1. **Given** a user has created multiple notebooks, **When** they
   navigate to the main interface, **Then** they see a list of all
   their notebooks with names and last-modified dates.
2. **Given** a notebook with saved content, **When** the user reopens
   it after closing the browser, **Then** all cells, outputs, and
   environment state are preserved.
3. **Given** a notebook the user no longer needs, **When** they delete
   it, **Then** the notebook and its associated container and data
   are removed, and it no longer appears in the notebook list.
4. **Given** a notebook, **When** the user renames it, **Then** the
   new name is reflected in the notebook list and persists.

---

### Edge Cases

- What happens when a container fails to start (e.g., out of memory
  or Docker daemon unavailable)? The user MUST see an actionable error
  message, not a blank screen or cryptic failure.
- What happens when a user executes a cell that runs indefinitely
  (infinite loop)? The user MUST be able to interrupt execution.
- What happens when the host has no GPU but the user attempts
  GPU operations? The system MUST surface a clear message that GPU
  is not available rather than crashing silently.
- What happens when disk space is exhausted by package installations?
  The user MUST be notified that the environment has reached its
  storage limit.
- What happens when the user's browser connection drops mid-execution?
  Cell execution MUST continue in the container, and results MUST be
  available when the user reconnects.
- What happens when two browser tabs open the same notebook? The
  system MUST either synchronize state or prevent concurrent editing
  with a clear message.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST provide a browser-based notebook interface
  accessible via standard web browsers (Chrome, Firefox, Safari, Edge).
- **FR-002**: Each notebook MUST execute code within its own isolated
  container environment, with no shared state between notebooks.
- **FR-003**: Users MUST be able to create, open, save, rename, and
  delete notebooks from the browser interface.
- **FR-004**: Users MUST be able to add, edit, delete, reorder, and
  execute code cells within a notebook.
- **FR-005**: System MUST support environment setup cells that execute
  package installation commands (e.g., `pip install <package>`) within
  the notebook's container.
- **FR-006**: Installed packages MUST persist in the notebook's
  container across sessions without requiring re-installation.
- **FR-007**: Containers MUST have full GPU passthrough access to all
  NVIDIA GPUs available on the host machine.
- **FR-008**: System MUST display cell execution output (text, errors,
  rich output) inline below the executed cell.
- **FR-009**: System MUST support both code cells and markdown/text
  cells for documentation within notebooks.
- **FR-010**: System MUST manage container lifecycle automatically —
  provisioning on notebook creation, starting on notebook open,
  and stopping after a configurable idle timeout.
- **FR-011**: Users MUST be able to interrupt a running cell execution.
- **FR-012**: System MUST persist notebook content (cells, outputs,
  metadata) durably so that data survives browser closure and
  container restarts.
- **FR-013**: System MUST provide visual indication of cell execution
  state (idle, running, completed, errored).
- **FR-014**: System MUST replicate Jupyter's default keyboard shortcut
  scheme exactly, including Shift+Enter (run and advance), Ctrl+Enter
  (run in place), Esc/Enter (command/edit mode switching), and all
  standard command-mode and edit-mode bindings. Users migrating from
  Jupyter MUST have zero re-learning curve for keyboard interactions.

### Key Entities

- **Notebook**: The primary user-facing document. Contains an ordered
  list of cells, metadata (name, created date, last modified), and a
  reference to its associated container environment.
- **Cell**: An individual block within a notebook. Has a type (code or
  markdown), content (source text), execution state, execution order
  number, and output (for code cells).
- **Container Environment**: An isolated execution environment
  associated with exactly one notebook. Includes a base image, installed
  packages, filesystem state, and GPU device access configuration.
- **Environment Configuration**: The set of package installation
  commands defined in setup cells. Serves as the declarative
  specification of the notebook's environment.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users can create a new notebook and execute their first
  code cell within 30 seconds of reaching the application (including
  container provisioning).
- **SC-002**: A notebook with 100 cells loads and becomes interactive
  within 5 seconds when reopened.
- **SC-003**: Package installation commands execute and complete at
  the same speed as running them directly in a terminal (no more than
  10% overhead).
- **SC-004**: GPU-accelerated workloads in a notebook container achieve
  at least 95% of the performance of running the same code directly
  on the host.
- **SC-005**: Notebook content and environment state survive browser
  closure, container restart, and application restart with zero data
  loss.
- **SC-006**: Users familiar with Jupyter can create a notebook,
  install a package, and run GPU-accelerated code without consulting
  documentation.
- **SC-007**: The system supports at least 10 concurrent notebooks
  with active containers on a single host without degradation.

## Assumptions

- Users are data scientists, ML engineers, or developers familiar
  with Jupyter notebooks and Python package management.
- The host machine runs Linux with Docker (or a compatible container
  runtime) installed and configured.
- NVIDIA GPUs with appropriate drivers and the NVIDIA Container
  Toolkit are installed on the host for GPU passthrough.
- Python is the primary execution language for v1. Support for
  additional languages (R, Julia) is out of scope.
- The application is intended for single-user or small-team use on
  a local or on-premises machine, not multi-tenant cloud deployment.
- Authentication and multi-user access control are out of scope for
  v1 (similar to how vanilla Jupyter runs without auth by default).
- Mobile browser support is out of scope for v1; the interface
  targets desktop browsers at 1024px+ viewport width.
