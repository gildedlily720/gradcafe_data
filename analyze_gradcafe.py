#!/usr/bin/env python3
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

def load_data():
    """Load and prepare GradCafe data"""
    try:
        # Try to read the data file
        df = pd.read_csv('all/all.csv')
        print("Successfully loaded data")
        return df
    except FileNotFoundError:
        print("Error: Could not find the data file. Please make sure you're in the correct directory.")
        return None

def analyze_yale_polisci(df):
    """Analyze Yale Political Science admissions data"""
    if df is None:
        return None, None, None
    
    # Filter for Yale Political Science
    yale_polisci = df[
        (df['uni_name'].str.contains('Yale', case=False, na=False)) & 
        (df['major'].str.contains('Political Science', case=False, na=False))
    ]
    
    print(f"\nFound {len(yale_polisci)} Yale Political Science entries")
    
    # Create summary of decisions
    decision_summary = yale_polisci['decision'].value_counts()
    
    # Calculate GRE stats based on new/old GRE format
    new_gre = yale_polisci[yale_polisci['is_new_gre'] == 1]
    old_gre = yale_polisci[yale_polisci['is_new_gre'] == 0]
    
    # GPA and GRE summaries by decision
    stats_summary = yale_polisci.groupby('decision').agg({
        'ugrad_gpa': ['mean', 'count'],
        'gre_verbal': ['mean', 'count'],
        'gre_quant': ['mean', 'count'],
        'gre_writing': ['mean', 'count']
    }).round(2)
    
    return yale_polisci, decision_summary, stats_summary

def plot_gre_scores(yale_data):
    """Create visualizations of GRE scores"""
    if yale_data is None or len(yale_data) == 0:
        print("No data available for plotting")
        return
    
    plt.figure(figsize=(10, 6))
    sns.scatterplot(
        data=yale_data,
        x='gre_verbal',
        y='gre_quant',
        hue='decision',
        style='decision'
    )
    plt.title('Yale Political Science: GRE Verbal vs Quantitative')
    plt.savefig('yale_gre_scores.png')
    plt.close()

def main():
    print("Starting GradCafe data analysis...")
    
    # Load data
    df = load_data()
    
    # Analyze data
    yale_data, decisions, stats = analyze_yale_polisci(df)
    
    if yale_data is not None:
        # Print results
        print("\nDecision Summary:")
        print(decisions)
        print("\nStats Summary:")
        print(stats)
        
        # Create visualization
        plot_gre_scores(yale_data)
        
        # Save results
        yale_data.to_csv('yale_polisci_results.csv', index=False)
        print("\nResults saved to yale_polisci_results.csv")
        print("GRE score plot saved as yale_gre_scores.png")

if __name__ == "__main__":
    main()