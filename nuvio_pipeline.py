name: Nuvio Backdrop Renderer

on:
  workflow_dispatch:
  # schedule:
  #   - cron: '0 2 * * *'

jobs:
  render_backdrops:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4
        
      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
          
      - name: Download Rendering Engine & Install Dependencies
        run: |
          # 1. Download the actual image engine from the original creator
          git clone https://github.com/bramst0ne/prism-wallpapers.git engine
          # 2. Copy those files into our main workspace so our script can see them
          cp -r engine/* .
          
          # 3. Install required software (both ours, and the engine's)
          python -m pip install --upgrade pip
          pip install requests Pillow
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
          if [ -f engine/requirements.txt ]; then pip install -r engine/requirements.txt; fi
          
      - name: Run Nuvio Automation Pipeline
        env:
          TMDB_API_KEY: ${{ secrets.TMDB_API_KEY }}
          FANART_API_KEY: ${{ secrets.FANART_API_KEY }}
          AIOMETADATA_URL: ${{ secrets.AIOMETADATA_URL }}
        run: python nuvio_pipeline.py
        
      - name: Commit and Push CDN Assets
        run: |
          git config --global user.name 'github-actions[bot]'
          git config --global user.email 'github-actions[bot]@users.noreply.github.com'
          git add collections/
          git commit -m "Refresh Nuvio Backdrops - [Automated Run]" || echo "No changes to commit"
          git push
