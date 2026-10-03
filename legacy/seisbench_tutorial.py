import obspy
import seisbench.models as sbm

def main():
    print("Loading basic example data via ObsPy...")
    # Load an example stream from obspy
    stream = obspy.read()
    # Pad the stream to ensure it is longer than the 3001 sample window required by PhaseNet
    endtime = max([tr.stats.endtime for tr in stream]) + 1.0
    stream.trim(endtime=endtime, pad=True, fill_value=0)
    print("Stream details (after padding):")
    print(stream)

    print("\nLoading pre-trained PhaseNet model (trained on ETHZ data)...")
    # ETHZ is a commonly available pre-trained weight set for PhaseNet in SeisBench
    model = sbm.PhaseNet.from_pretrained("ethz")
    
    print("\nAnnotating the stream...")
    # The annotate function applies the model to the data and returns continuous prediction probabilities
    annotations = model.annotate(stream)
    
    print("\nResulting annotations (Prediction probabilities for P, S, and Noise):")
    print(annotations)

if __name__ == "__main__":
    main()
