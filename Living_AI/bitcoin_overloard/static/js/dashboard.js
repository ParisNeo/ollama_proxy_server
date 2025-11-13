// Bitcoin Overloard Dashboard - Real-time Intelligence Updates
// UPGRADED: Removed all demo/placeholder data, connects to real API

class BitcoinDashboard {
    constructor() {
        // Try to detect if we're on GitHub Pages or local server
        const isGitHubPages = window.location.hostname.includes('github.io');
        this.apiBase = isGitHubPages ? 'http://localhost:8091' : '';
        this.apiEndpoint = `${this.apiBase}/api/data`;
        this.updateInterval = 10000; // 10 seconds
        this.chart = null;
        this.isConnected = false;
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.initPriceChart();
        this.startAutoUpdate();
        this.loadInitialData();
    }

    setupEventListeners() {
        // Chart range buttons
        document.querySelectorAll('.chart-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.chart-btn').forEach(b => b.classList.remove('active'));
                e.target.classList.add('active');
                this.updateChartRange(e.target.dataset.range);
            });
        });

        // Refresh reports button
        document.getElementById('refresh-reports')?.addEventListener('click', () => {
            this.loadReports();
        });
    }

    initPriceChart() {
        const ctx = document.getElementById('price-chart');
        if (!ctx) return;

        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    {
                        label: 'Bitcoin Price',
                        data: [],
                        borderColor: '#f7931a',
                        backgroundColor: 'rgba(247, 147, 26, 0.1)',
                        tension: 0.4,
                        fill: true,
                        pointRadius: 0,
                        borderWidth: 2
                    },
                    {
                        label: 'AI Prediction',
                        data: [],
                        borderColor: '#4a90e2',
                        backgroundColor: 'rgba(74, 144, 226, 0.1)',
                        tension: 0.4,
                        fill: true,
                        pointRadius: 0,
                        borderWidth: 2,
                        borderDash: [5, 5]
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        labels: {
                            color: '#ffffff'
                        }
                    },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                    }
                },
                scales: {
                    x: {
                        grid: {
                            color: 'rgba(255, 255, 255, 0.1)'
                        },
                        ticks: {
                            color: '#a8a8a8'
                        }
                    },
                    y: {
                        grid: {
                            color: 'rgba(255, 255, 255, 0.1)'
                        },
                        ticks: {
                            color: '#a8a8a8',
                            callback: function(value) {
                                return '$' + value.toLocaleString();
                            }
                        }
                    }
                },
                interaction: {
                    mode: 'nearest',
                    axis: 'x',
                    intersect: false
                }
            }
        });
    }

    async loadInitialData() {
        // Try to load from real API only - NO demo data fallback
        try {
            await this.fetchData();
        } catch (error) {
            console.log('API not available - dashboard requires live server');
            this.showApiUnavailable();
        }
    }

    async fetchData() {
        try {
            const response = await fetch(this.apiEndpoint);
            if (!response.ok) throw new Error('API not available');
            
            const data = await response.json();
            this.updateDashboard(data);
            this.setConnectionStatus(true);
        } catch (error) {
            console.error('Error fetching data:', error);
            this.setConnectionStatus(false);
            throw error;
        }
    }

    showApiUnavailable() {
        // Show message instead of demo data
        const containers = [
            'btc-price', 'ai-sentiment', 'market-trend', 'consensus-score',
            'predictions-list', 'patterns-list', 'signals-list', 'reports-list'
        ];
        
        containers.forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.innerHTML = '<div style="color: #888; padding: 20px; text-align: center;">' +
                    '<i class="fas fa-exclamation-triangle"></i><br>' +
                    'Live dashboard requires server running at http://localhost:8091<br>' +
                    '<a href="http://localhost:8091" style="color: #667eea; margin-top: 10px; display: inline-block;">' +
                    'Access Full Dashboard →</a></div>';
            }
        });
        
        this.setConnectionStatus(false);
    }

    updateDashboard(data) {
        // Update price and change
        if (data.btc_price) {
            const priceEl = document.getElementById('btc-price');
            if (priceEl) {
                priceEl.textContent = `$${data.btc_price.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
            }
        }

        if (data.btc_change_24h !== undefined) {
            const changeElement = document.querySelector('.change-value');
            if (changeElement) {
                const change = data.btc_change_24h;
                changeElement.textContent = `${change > 0 ? '+' : ''}${change.toFixed(2)}%`;
                changeElement.className = `change-value ${change >= 0 ? 'positive' : 'negative'}`;
            }
        }

        // Update AI sentiment
        if (data.ai_sentiment) {
            const sentimentEl = document.getElementById('ai-sentiment');
            if (sentimentEl) sentimentEl.textContent = data.ai_sentiment;
        }

        if (data.sentiment_score !== undefined) {
            const fillElement = document.getElementById('sentiment-fill');
            if (fillElement) {
                fillElement.style.width = `${data.sentiment_score}%`;
                
                // Color based on score
                if (data.sentiment_score >= 70) {
                    fillElement.style.background = '#2ecc71';
                } else if (data.sentiment_score >= 40) {
                    fillElement.style.background = '#f39c12';
                } else {
                    fillElement.style.background = '#e74c3c';
                }
            }
        }

        // Update market trend
        if (data.market_trend) {
            const trendEl = document.getElementById('market-trend');
            if (trendEl) trendEl.textContent = data.market_trend;
        }

        if (data.trend_confidence !== undefined) {
            const confEl = document.getElementById('trend-confidence');
            if (confEl) {
                confEl.textContent = `Confidence: ${data.trend_confidence}%`;
            }
        }

        // Update consensus
        if (data.consensus_score !== undefined) {
            const consensusEl = document.getElementById('consensus-score');
            if (consensusEl) consensusEl.textContent = `${data.consensus_score}%`;
        }

        if (data.active_models !== undefined) {
            const modelsEl = document.getElementById('active-models');
            if (modelsEl) {
                modelsEl.textContent = `${data.active_models} models active`;
            }
        }

        // Update predictions
        if (data.predictions) {
            this.updatePredictions(data.predictions);
        }

        // Update patterns
        if (data.patterns) {
            this.updatePatterns(data.patterns);
        }

        // Update signals
        if (data.signals) {
            this.updateSignals(data.signals);
        }

        // Update reports
        if (data.reports) {
            this.updateReports(data.reports);
        }

        // Update chart
        if (data.chart_data) {
            this.updateChart(data.chart_data);
        }

        // Update last update time
        this.updateLastUpdateTime();
    }

    updatePredictions(predictions) {
        const container = document.getElementById('predictions-list');
        if (!container) return;
        
        container.innerHTML = '';

        predictions.forEach(pred => {
            const item = document.createElement('div');
            item.className = 'prediction-item';
            item.innerHTML = `
                <span class="model-name">${pred.model || pred.model_name || 'Unknown Model'}</span>
                <span class="prediction-value">${pred.value || pred.prediction || 'N/A'}</span>
            `;
            container.appendChild(item);
        });
    }

    updatePatterns(patterns) {
        const container = document.getElementById('patterns-list');
        if (!container) return;
        
        container.innerHTML = '';

        patterns.forEach(pattern => {
            const item = document.createElement('div');
            item.className = 'pattern-item';
            const name = pattern.name || pattern.pattern || 'Unknown Pattern';
            const detected = pattern.detected || pattern.timestamp || 'Unknown';
            const confidence = pattern.confidence || pattern.importance || 'Medium';
            item.innerHTML = `
                <div><strong>${name}</strong></div>
                <div style="color: #a8a8a8; font-size: 12px; margin-top: 4px;">
                    Detected: ${detected} | Confidence: ${confidence}
                </div>
            `;
            container.appendChild(item);
        });
    }

    updateSignals(signals) {
        const container = document.getElementById('signals-list');
        if (!container) return;
        
        container.innerHTML = '';

        signals.forEach(signal => {
            const item = document.createElement('div');
            item.className = 'signal-item';
            const time = signal.time || signal.timestamp || '--:--';
            const text = signal.text || signal.insight || signal.signal || 'No signal text';
            item.innerHTML = `
                <span class="signal-time">${time}</span>
                <span class="signal-text">${text}</span>
            `;
            container.appendChild(item);
        });
    }

    updateReports(reports) {
        const container = document.getElementById('reports-list');
        if (!container) return;
        
        container.innerHTML = '';

        reports.forEach(report => {
            const item = document.createElement('div');
            item.className = 'report-item';
            const name = report.name || report.report_type || 'Unknown Report';
            const date = report.date || report.timestamp || 'Unknown';
            const time = report.time || '';
            item.innerHTML = `
                <span class="report-name">${name}</span>
                <span class="report-date">${date} ${time}</span>
            `;
            container.appendChild(item);
        });
    }

    updateChart(chartData) {
        if (!this.chart) return;

        this.chart.data.labels = chartData.labels || [];
        this.chart.data.datasets[0].data = chartData.prices || [];
        this.chart.data.datasets[1].data = chartData.predictions || [];
        this.chart.update('none'); // Update without animation for smooth real-time updates
    }

    updateChartRange(range) {
        // Fetch new data for the selected range from real API
        console.log('Chart range changed to:', range);
        // In a real implementation, fetch new data for the selected range
        this.fetchData();
    }

    loadReports() {
        console.log('Refreshing reports...');
        this.fetchData();
    }

    setConnectionStatus(connected) {
        this.isConnected = connected;
        const statusElement = document.getElementById('connection-status');
        if (!statusElement) return;
        
        if (connected) {
            statusElement.innerHTML = '<i class="fas fa-circle"></i> Connected';
            statusElement.className = 'status-indicator connected';
        } else {
            statusElement.innerHTML = '<i class="fas fa-circle"></i> API Unavailable';
            statusElement.className = 'status-indicator disconnected';
        }
    }

    updateLastUpdateTime() {
        const now = new Date();
        const timeString = now.toLocaleTimeString('en-US', { 
            hour: '2-digit', 
            minute: '2-digit',
            second: '2-digit'
        });
        const updateEl = document.getElementById('last-update');
        if (updateEl) {
            updateEl.textContent = `Last Update: ${timeString}`;
        }
    }

    startAutoUpdate() {
        setInterval(() => {
            this.fetchData().catch(() => {
                // If fetch fails, show API unavailable message (no demo data)
                this.showApiUnavailable();
            });
        }, this.updateInterval);
    }
}

// Initialize dashboard when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    // Only initialize if dashboard elements exist (not on landing page)
    if (document.getElementById('btc-price') || document.getElementById('price-chart')) {
        new BitcoinDashboard();
    }
});

