// SpotSync Frontend Application
class SpotSyncApp {
    constructor() {
        this.baseUrl = window.location.origin;
        this.authToken = null;
        this.activeWebSockets = new Map(); // jobId -> WebSocket
        this.jobPollInterval = null;
        this.jobLogs = new Map(); // jobId -> array of log entries
        this.currentJobId = null; // Currently displayed job in modal
        this.terminalLogs = []; // Global terminal logs
        this.autoScroll = true; // Auto-scroll terminal
        this.maxTerminalLines = 2000; // Max lines to keep in terminal

        // DOM Elements
        this.loginScreen = document.getElementById('login-screen');
        this.dashboardScreen = document.getElementById('dashboard-screen');
        this.loginBtn = document.getElementById('login-btn');
        this.logoutBtn = document.getElementById('logout-btn');
        this.usernameInput = document.getElementById('username');
        this.passwordInput = document.getElementById('password');
        this.spotifyUrlInput = document.getElementById('spotify-url');
        this.submitJobBtn = document.getElementById('submit-job-btn');
        this.activeJobsList = document.getElementById('active-jobs-list');
        this.recentJobsList = document.getElementById('recent-jobs-list');
        this.jobModal = document.getElementById('job-modal');
        this.closeModalBtn = document.getElementById('close-modal-btn');
        this.jobDetails = document.getElementById('job-details');
        this.toastContainer = document.getElementById('toast-container');

        // Terminal Elements
        this.terminalOutput = document.getElementById('terminal-output');
        this.clearTerminalBtn = document.getElementById('clear-terminal-btn');
        this.toggleTerminalBtn = document.getElementById('toggle-terminal-btn');
        this.autoScrollIcon = document.getElementById('auto-scroll-icon');

        this.init();
    }

    init() {
        // Check if already logged in (from sessionStorage)
        const savedToken = sessionStorage.getItem('spotsync_token');
        if (savedToken) {
            this.authToken = savedToken;
            this.showDashboard();
            this.loadJobs();
        } else {
            this.showLogin();
        }

        this.bindEvents();
    }

    bindEvents() {
        // Login
        this.loginBtn.addEventListener('click', () => this.handleLogin());
        this.passwordInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.handleLogin();
        });

        // Logout
        this.logoutBtn.addEventListener('click', () => this.handleLogout());

        // Job submission
        this.submitJobBtn.addEventListener('click', () => this.submitJob());
        this.spotifyUrlInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.submitJob();
        });

        // Modal
        this.closeModalBtn.addEventListener('click', () => this.hideModal());
        this.jobModal.addEventListener('click', (e) => {
            if (e.target === this.jobModal) this.hideModal();
        });

        // Terminal controls
        this.clearTerminalBtn.addEventListener('click', () => this.clearTerminal());
        this.toggleTerminalBtn.addEventListener('click', () => this.toggleAutoScroll());
    }

    // Authentication
    async handleLogin() {
        const username = this.usernameInput.value;
        const password = this.passwordInput.value;

        if (!password) {
            this.showToast('Please enter password', 'error');
            return;
        }

        try {
            // Create Basic Auth token
            const token = btoa(`${username}:${password}`);
            const response = await fetch(`${this.baseUrl}/api/auth/login`, {
                method: 'POST',
                headers: {
                    'Authorization': `Basic ${token}`
                }
            });

            if (!response.ok) {
                throw new Error('Invalid credentials');
            }

            // Save token and switch to dashboard
            this.authToken = token;
            sessionStorage.setItem('spotsync_token', token);
            this.addTerminalLine('User logged in successfully', 'success');
            this.showDashboard();
            this.loadJobs();
            this.showToast('Login successful', 'success');

        } catch (error) {
            this.showToast(error.message || 'Login failed', 'error');
        }
    }

    handleLogout() {
        this.authToken = null;
        sessionStorage.removeItem('spotsync_token');

        // Close all WebSocket connections
        this.activeWebSockets.forEach(ws => ws.close());
        this.activeWebSockets.clear();

        // Clear poll interval
        if (this.jobPollInterval) {
            clearInterval(this.jobPollInterval);
            this.jobPollInterval = null;
        }

        this.addTerminalLine('User logged out', 'info');
        this.showLogin();
        this.showToast('Logged out', 'info');
    }

    // UI Navigation
    showLogin() {
        this.loginScreen.classList.remove('hidden');
        this.dashboardScreen.classList.add('hidden');
        this.passwordInput.value = '';
    }

    showDashboard() {
        this.loginScreen.classList.add('hidden');
        this.dashboardScreen.classList.remove('hidden');

        // Start polling for job updates
        if (!this.jobPollInterval) {
            this.jobPollInterval = setInterval(() => this.loadJobs(), 10000); // Every 10 seconds
        }

        // Add welcome message to terminal
        if (this.terminalLogs.length === 0) {
            this.addTerminalLine('SpotSync terminal ready - waiting for downloads...', 'info');
        }
    }

    // Job Management
    async submitJob() {
        const query = this.spotifyUrlInput.value.trim();

        if (!query) {
            this.showToast('Please enter a Spotify URL', 'error');
            return;
        }

        try {
            this.submitJobBtn.disabled = true;
            this.submitJobBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Submitting...';

            const response = await fetch(`${this.baseUrl}/api/jobs`, {
                method: 'POST',
                headers: {
                    'Authorization': `Basic ${this.authToken}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ query })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Failed to submit job');
            }

            const job = await response.json();
            this.spotifyUrlInput.value = '';
            this.showToast('Job submitted successfully', 'success');
            this.loadJobs(); // Refresh job list
            this.connectToJobWebSocket(job.id); // Connect for live updates
            // Add to terminal
            this.addTerminalLine(`New job submitted: ${query.substring(0, 50)}...`, 'job-start', new Date(), job.id);

        } catch (error) {
            this.showToast(error.message, 'error');
        } finally {
            this.submitJobBtn.disabled = false;
            this.submitJobBtn.innerHTML = '<i class="fas fa-cloud-download-alt"></i> Start Download';
        }
    }

    async loadJobs() {
        try {
            const response = await fetch(`${this.baseUrl}/api/jobs?limit=20`, {
                headers: {
                    'Authorization': `Basic ${this.authToken}`
                }
            });

            if (!response.ok) throw new Error('Failed to load jobs');

            const jobs = await response.json();

            // Separate active and completed jobs
            const activeJobs = jobs.filter(job => job.status === 'pending' || job.status === 'running');
            const recentJobs = jobs.filter(job => job.status === 'done' || job.status === 'failed').slice(0, 10);

            this.renderJobsList(this.activeJobsList, activeJobs, true);
            this.renderJobsList(this.recentJobsList, recentJobs, false);

            // Connect WebSockets for active jobs
            activeJobs.forEach(job => {
                if (!this.activeWebSockets.has(job.id)) {
                    this.connectToJobWebSocket(job.id);
                }
            });

        } catch (error) {
            console.error('Error loading jobs:', error);
        }
    }

    renderJobsList(container, jobs, showProgress) {
        if (jobs.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-${showProgress ? 'clock' : 'music'}"></i>
                    <p>No ${showProgress ? 'active' : 'recent'} jobs</p>
                </div>
            `;
            return;
        }

        container.innerHTML = jobs.map(job => this.createJobCard(job, showProgress)).join('');

        // Add click handlers to view job details
        container.querySelectorAll('.job-card').forEach(card => {
            const jobId = card.dataset.jobId;
            card.addEventListener('click', () => this.showJobDetails(jobId));
        });
    }

    createJobCard(job, showProgress) {
        const date = new Date(job.created_at).toLocaleString();
        const playlistName = job.playlist_name || 'Single Track';

        let progressHtml = '';
        if (showProgress) {
            progressHtml = `
                <div class="job-progress">
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: ${job.status === 'done' ? '100' : '50'}%"></div>
                    </div>
                    <div class="progress-text">${job.track_count} tracks</div>
                </div>
            `;
        }

        return `
            <div class="job-card ${job.status}" data-job-id="${job.id}">
                <div class="job-header">
                    <div class="job-title">${this.escapeHtml(playlistName)}</div>
                    <div class="job-actions">
                        <span class="job-status status-${job.status}">${job.status}</span>
                        <button class="btn btn-icon delete-job-btn" onclick="app.deleteJob('${job.id}', event)">
                            <i class="fas fa-trash"></i>
                        </button>
                    </div>
                </div>
                <div class="job-meta">
                    <span><i class="fas fa-calendar"></i> ${date}</span>
                    <span><i class="fas fa-music"></i> ${job.track_count} tracks</span>
                </div>
                ${progressHtml}
            </div>
        `;
    }

    // WebSocket Management
    connectToJobWebSocket(jobId) {
        // Close existing connection if any
        if (this.activeWebSockets.has(jobId)) {
            this.activeWebSockets.get(jobId).close();
        }

        // Determine WebSocket URL (handle both ws:// and wss://)
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/jobs/${jobId}`;

        const ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            console.log(`WebSocket connected for job ${jobId}`);
            this.activeWebSockets.set(jobId, ws);
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.handleWebSocketMessage(jobId, data);
            } catch (error) {
                console.error('Error parsing WebSocket message:', error);
            }
        };

        ws.onclose = () => {
            console.log(`WebSocket disconnected for job ${jobId}`);
            this.activeWebSockets.delete(jobId);

            // Try to reconnect after delay (exponential backoff)
            setTimeout(() => {
                if (!this.activeWebSockets.has(jobId)) {
                    this.connectToJobWebSocket(jobId);
                }
            }, 5000);
        };

        ws.onerror = (error) => {
            console.error(`WebSocket error for job ${jobId}:`, error);
        };
    }

    handleWebSocketMessage(jobId, data) {
        switch (data.event) {
            case 'initial_state':
                // Initial job state - could update UI
                break;

            case 'job_updated':
                // Job metadata updated (e.g., track count for playlists)
                this.loadJobs(); // Refresh job list
                break;

            case 'track_update':
                // Individual track update
                this.showToast(`Track update: ${data.status}`, 'info');
                this.loadJobs(); // Refresh job list
                // Add to terminal
                this.addTerminalLine(`Track ${data.track_id.substring(0, 8)}: ${data.status} (${data.percent}%)`, 'info', new Date(data.timestamp * 1000), jobId);
                break;

            case 'spotdl_output':
                this.handleSpotdlOutput(jobId, data);
                break;

            case 'job_complete':
                // Job completed
                const statusMsg = `Job ${data.status === 'done' ? 'completed successfully' : 'failed'}`;
                this.showToast(statusMsg, data.status === 'done' ? 'success' : 'error');
                this.loadJobs(); // Refresh job list
                // Add to terminal
                this.addTerminalLine(`Job ${jobId.substring(0, 8)}: ${statusMsg}`, data.status === 'done' ? 'success' : 'error', new Date(data.timestamp * 1000), jobId);

                // Close WebSocket for completed job
                if (this.activeWebSockets.has(jobId)) {
                    this.activeWebSockets.get(jobId).close();
                    this.activeWebSockets.delete(jobId);
                }
                break;
        }
    }

    handleSpotdlOutput(jobId, data) {
        // Store log entry
        if (!this.jobLogs.has(jobId)) {
            this.jobLogs.set(jobId, []);
        }
        const logs = this.jobLogs.get(jobId);
        logs.push({
            timestamp: data.timestamp,
            trackId: data.track_id,
            output: data.output
        });
        // Keep only last 1000 lines to prevent memory bloat
        if (logs.length > 1000) {
            logs.shift();
        }
        // Update UI if job details modal is open for this job
        this.updateJobLogsUI(jobId);

        // Also add to terminal view
        this.addTerminalLine(data.output, 'info', new Date(data.timestamp * 1000), jobId);
    }

    updateJobLogsUI(jobId) {
        // Only update if job details modal is open for this job
        if (this.currentJobId === jobId) {
            // Reload job details to show updated logs
            this.showJobDetails(jobId);
        }
    }

    // Terminal Methods
    addTerminalLine(text, type = 'info', timestamp = null, jobId = null) {
        const line = {
            text: text.trim(),
            type: type,
            timestamp: timestamp || new Date(),
            jobId: jobId
        };

        this.terminalLogs.push(line);

        // Keep only max lines
        if (this.terminalLogs.length > this.maxTerminalLines) {
            this.terminalLogs.shift();
        }

        // Update terminal UI
        this.updateTerminalUI();

        // Auto-scroll if enabled
        if (this.autoScroll) {
            this.scrollTerminalToBottom();
        }
    }

    updateTerminalUI() {
        if (!this.terminalOutput) return;

        const lines = this.terminalLogs.map(line => {
            const time = line.timestamp.toLocaleTimeString();
            const jobPrefix = line.jobId ? `[Job:${line.jobId.substring(0, 8)}] ` : '';
            const typeClass = `terminal-line ${line.type}`;
            return `<div class="${typeClass}">[${time}] ${jobPrefix}${this.escapeHtml(line.text)}</div>`;
        });

        this.terminalOutput.innerHTML = lines.join('');

        // Auto-scroll if enabled
        if (this.autoScroll) {
            this.scrollTerminalToBottom();
        }
    }

    scrollTerminalToBottom() {
        if (this.terminalOutput) {
            this.terminalOutput.scrollTop = this.terminalOutput.scrollHeight;
        }
    }

    clearTerminal() {
        this.terminalLogs = [];
        this.updateTerminalUI();
        this.addTerminalLine('Terminal cleared.', 'info');
    }

    toggleAutoScroll() {
        this.autoScroll = !this.autoScroll;
        this.autoScrollIcon.className = this.autoScroll ? 'fas fa-arrow-down' : 'fas fa-pause';
        this.autoScrollIcon.title = this.autoScroll ? 'Auto-scroll enabled' : 'Auto-scroll disabled';

        if (this.autoScroll) {
            this.scrollTerminalToBottom();
        }
    }

    // Job Details Modal
    async showJobDetails(jobId) {
        this.currentJobId = jobId;
        try {
            const response = await fetch(`${this.baseUrl}/api/jobs/${jobId}`, {
                headers: {
                    'Authorization': `Basic ${this.authToken}`
                }
            });

            if (!response.ok) throw new Error('Failed to load job details');

            const job = await response.json();
            this.renderJobDetails(job);
            this.showModal();

        } catch (error) {
            this.showToast(error.message, 'error');
        }
    }

    renderJobDetails(job) {
        const date = new Date(job.created_at).toLocaleString();
        const playlistName = job.playlist_name || 'Single Track';

        // Group tracks by status
        const tracksByStatus = {
            queued: job.tracks.filter(t => t.status === 'queued'),
            downloading: job.tracks.filter(t => t.status === 'downloading'),
            done: job.tracks.filter(t => t.status === 'done'),
            failed: job.tracks.filter(t => t.status === 'failed'),
            imported: job.tracks.filter(t => t.lidarr_import_status === 'imported')
        };

        const tracksHtml = Object.entries(tracksByStatus)
            .filter(([status, tracks]) => tracks.length > 0)
            .map(([status, tracks]) => `
                <div class="track-status-group">
                    <h3>${status.toUpperCase()} (${tracks.length})</h3>
                    ${tracks.map(track => this.createTrackItem(track)).join('')}
                </div>
            `).join('');

        const logs = this.jobLogs.get(job.id) || [];
        const logsHtml = logs.length > 0 ? `
            <div class="job-logs">
                <h3>Download Logs</h3>
                <pre class="logs-content">${logs.map(log => `[${new Date(log.timestamp * 1000).toLocaleTimeString()}] ${this.escapeHtml(log.output)}`).join('\n')}</pre>
            </div>
        ` : '';

        this.jobDetails.innerHTML = `
            <div class="job-detail-header">
                <h2>${this.escapeHtml(playlistName)}</h2>
                <div class="job-detail-meta">
                    <p><strong>Status:</strong> <span class="status-${job.status}">${job.status}</span></p>
                    <p><strong>Submitted:</strong> ${date}</p>
                    <p><strong>URL:</strong> <a href="${job.spotify_url}" target="_blank">${job.spotify_url}</a></p>
                    <p><strong>Tracks:</strong> ${job.track_count}</p>
                </div>
            </div>
            <div class="tracks-list">
                ${tracksHtml}
            </div>
            ${logsHtml}
            <div class="job-actions-modal">
                ${job.status === 'failed' ? `
                    <button class="btn btn-primary" onclick="app.retryJob('${job.id}')">
                        <i class="fas fa-redo"></i> Retry Failed Tracks
                    </button>
                ` : ''}
                <button class="btn btn-danger" onclick="app.deleteJob('${job.id}')">
                    <i class="fas fa-trash"></i> Delete Job
                </button>
            </div>
        `;
    }

    createTrackItem(track) {
        return `
            <div class="track-item ${track.status}">
                <div class="track-info">
                    <div class="track-title">${this.escapeHtml(track.title || 'Unknown')}</div>
                    <div class="track-artist">${this.escapeHtml(track.artist || 'Unknown')}</div>
                    ${track.album ? `<div class="track-album">${this.escapeHtml(track.album)}</div>` : ''}
                </div>
                <div class="track-status status-${track.status}">
                    ${track.status}
                    ${track.lidarr_import_status === 'imported' ? ' ✓' : ''}
                </div>
            </div>
        `;
    }

    async retryJob(jobId) {
        try {
            const response = await fetch(`${this.baseUrl}/api/jobs/${jobId}/retry`, {
                method: 'POST',
                headers: {
                    'Authorization': `Basic ${this.authToken}`
                }
            });

            if (!response.ok) throw new Error('Failed to retry job');

            this.showToast('Retrying failed tracks', 'info');
            this.loadJobs();
            this.hideModal();
            // Add to terminal
            this.addTerminalLine(`Job ${jobId.substring(0, 8)}: Retrying failed tracks`, 'info', new Date(), jobId);

        } catch (error) {
            this.showToast(error.message, 'error');
        }
    }

    async deleteJob(jobId, event = null) {
        // Stop event propagation to prevent opening job details
        if (event) {
            event.stopPropagation();
            event.preventDefault();
        }

        // Confirmation dialog
        if (!confirm('Are you sure you want to delete this job? This action cannot be undone.')) {
            return;
        }

        try {
            const response = await fetch(`${this.baseUrl}/api/jobs/${jobId}`, {
                method: 'DELETE',
                headers: {
                    'Authorization': `Basic ${this.authToken}`
                }
            });

            if (!response.ok) throw new Error('Failed to delete job');

            // Close WebSocket connection if open
            if (this.activeWebSockets.has(jobId)) {
                this.activeWebSockets.get(jobId).close();
                this.activeWebSockets.delete(jobId);
            }

            // Clear logs for this job
            this.jobLogs.delete(jobId);

            // If modal is open for this job, close it
            if (this.currentJobId === jobId) {
                this.hideModal();
            }

            this.showToast('Job deleted successfully', 'success');
            this.loadJobs();
            // Add to terminal
            this.addTerminalLine(`Job ${jobId.substring(0, 8)} deleted`, 'warning', new Date(), jobId);

        } catch (error) {
            this.showToast(error.message, 'error');
        }
    }

    // Modal Controls
    showModal() {
        this.jobModal.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
    }

    hideModal() {
        this.jobModal.classList.add('hidden');
        document.body.style.overflow = '';
        this.jobDetails.innerHTML = '';
        this.currentJobId = null;
    }

    // Utility Methods
    showToast(message, type = 'info') {
        const icons = {
            success: 'fas fa-check-circle',
            error: 'fas fa-exclamation-circle',
            info: 'fas fa-info-circle'
        };

        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.innerHTML = `
            <i class="${icons[type]}"></i>
            <span>${this.escapeHtml(message)}</span>
        `;

        this.toastContainer.appendChild(toast);

        // Auto-remove after 5 seconds
        setTimeout(() => {
            toast.style.animation = 'slideIn 0.3s ease reverse';
            setTimeout(() => {
                if (toast.parentNode) {
                    toast.parentNode.removeChild(toast);
                }
            }, 300);
        }, 5000);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize app when DOM is ready
let app;
document.addEventListener('DOMContentLoaded', () => {
    app = new SpotSyncApp();
    window.app = app; // Make available globally for onclick handlers
});