import matplotlib.cm as cm
import shap  # package used to calculate Shap values
import operator
from sklearn.model_selection import GridSearchCV
from sklearn.metrics import precision_recall_curve
import collections
from sklearn.feature_selection import RFE
import argparse
import pandas as pd
import pickle
from sklearn.metrics import confusion_matrix, average_precision_score
import sklearn.metrics as metrics
from sklearn import svm
import sys
from rdkit import Chem
from sklearn.model_selection import cross_val_predict, cross_val_score
from sklearn.pipeline import Pipeline, FeatureUnion
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from modtox.ML.descriptors_2D_ligand import *
from modtox.ML.external_descriptors import *
from modtox.docking.glide import analyse as md
from modtox.ML.imputer import NewImputer
import modtox.ML.classifiers as cl
import modtox.ML.visualization as vs
import modtox.Helpers.helpers as hp
import modtox.ML.app_domain as dom
import modtox.ML.distributions as distributions

TITLE_MOL = "molecules"
COLUMNS_TO_EXCLUDE = [ "Lig#", "Title", "Rank", "Conf#", "Pose#"]
LABELS = "labels"
CLF = ["SVM", "XGBOOST", "KN", "TREE", "NB", "NB_final"]


class GenericModel(object):

    def __init__(self, active, inactive, clf, filename_model, csv=None, pb=False, fp=False, descriptors=False, MACCS=True, columns=None, tpot=False, cv=None, debug=False):
        self.active = active
        self.inactive = inactive
        self.pb = pb
        self.fp = fp
        self.descriptors = descriptors
        self.MACCS = MACCS
        self.external_data = csv
        self.columns = columns
        self.data = self._load_training_set()
        self.features = self.data.iloc[:, :-1]
        self.labels = self.data.iloc[:, -1]
        self.filename_model = filename_model
        self.tpot = tpot
        self.cv = self.n_final_active if not cv else cv
        self.clf = cl.retrieve_classifier(clf, self.tpot, cv=self.cv, fast=True)
        self.imputer = 'column_based'
        self.debug = debug


    def _load_training_set(self):
        """
        Separate between train and test dataframe
    
        Input:
            :input_file: str
            :sdf_property: str
        Output:
            :xtrain: Pandas DataFrame with molecules for training
            :xtest: Pandas DataFrame with molecules for testing
            :ytrain: Pandas Dataframe with labels for training
            :ytest: Pandas DataFrame with labels for testing
        """
        assert self.active, "--active flag must be given for analysis"        
        assert self.inactive, "--inactive flag must be given for analysis"        
        print(self.active, self.inactive)
        actives = [ mol for mol in Chem.SDMolSupplier(self.active) if mol ]
        inactives = [ mol for mol in Chem.SDMolSupplier(self.inactive) if mol ]
            

        self.n_initial_active = len([mol for mol in Chem.SDMolSupplier(self.active)])
        self.n_initial_inactive = len([mol for mol in Chem.SDMolSupplier(self.inactive)])
        print("Active, Inactive")
        print(self.n_initial_active, self.n_initial_inactive)

        self.n_final_active = len(actives)
        self.n_final_inactive = len(inactives)
        print("Read Active, Read Inactive")
        print(self.n_final_active, self.n_final_inactive)

        #Do not handle tautomers with same molecule Name
        self.mol_names = []
        actives_non_repited = [] 
        inactives_non_repited = []
        for mol in actives:
            mol_name = mol.GetProp("_Name")
            if mol_name not in self.mol_names:
                self.mol_names.append(mol_name)
                actives_non_repited.append(mol)
        for mol in inactives:
            mol_name = mol.GetProp("_Name")
            if mol_name not in self.mol_names:
                self.mol_names.append(mol_name)
                inactives_non_repited.append(mol)

        #Main Dataframe
        actives_df = pd.DataFrame({TITLE_MOL: actives_non_repited })
        inactives_df =  pd.DataFrame({TITLE_MOL: inactives_non_repited })

        actives_df[LABELS] = [1,] * actives_df.shape[0]
        inactives_df[LABELS] = [0,] * inactives_df.shape[0]
    
        molecules = pd.concat([actives_df, inactives_df])
    
        self.data = molecules

        print("Non Repited Active, Non Repited Inactive")
        print(actives_df.shape[0], inactives_df.shape[0])

        print("Shape Dataset")
        print(self.data.shape[0])
    
        return self.data

    def __fit_transform__(self, X, predict=False, exclude=COLUMNS_TO_EXCLUDE):
        molecular_data = [ TITLE_MOL, ]
        
        numeric_features = []
        features = []
        if self.pb:
            if self.fp:
                numeric_features.extend('fingerprint')
                features.extend([('fingerprint', Fingerprints())])
            if self.descriptors:
                numeric_features.extend('descriptors')
                features.extend([('descriptors', Descriptors())])
            if self.MACCS:
                numeric_features.extend('fingerprintMACCS')
                features.extend([('fingerprintMACCS', Fingerprints_MACS())])

        if self.external_data:
            numeric_features.extend(['external_descriptors'])
            features.extend([('external_descriptors', ExternalData(self.external_data, self.mol_names, exclude=exclude))])

        molec_transformer = FeatureUnion(features)
       
        preprocessor = ColumnTransformer(
            transformers=[
                    ('mol', molec_transformer, molecular_data)])
        
        pre = Pipeline(steps=[('transformer', preprocessor)])
        X_trans = pre.fit_transform(X)
        #extracting molecules not docked 
        tmp = ExternalData(self.external_data, self.mol_names, exclude=exclude)
        mols_not_docked = tmp.retrieve_mols_not_docked(self.mol_names)
        return X_trans, mols_not_docked 
 
    def saving_model(self, scaler):
        f = open(os.path.join("model", self.filename_model), 'wb')

        if self.is_stack_model():
            if self.tpot:
                for clf in self.clf:
                    pickle.dump(clf.fitted_pipeline_, f)
                pickle.dump(scaler, f)
            else:
                for clf in self.clf[:-1]:
                    clf.fit(self.x_train_trans, self.labels)
                    pickle.dump(clf, f)
                    #and for the last model
                preds_train = np.array([ cross_val_predict(c, self.x_train_trans, self.labels, cv=self.cv) for c in self.clf[:-1 ]])
                preds_proba_train = np.array([ cross_val_predict(c, self.x_train_trans, self.labels, cv=self.cv, method='predict_proba') for c in self.clf[:-1 ]])
                #X = np.hstack( [self.x_train_trans, np.transpose([x[:,0] for x in preds_proba_train])])
                X = np.hstack( [self.x_train_trans, preds_train.T])
                
                last_model = self.clf[-1].fit(X, self.labels)
                pickle.dump(last_model, f)
                pickle.dump(scaler, f)
        else:
            if self.tpot:
                pickle.dump(self.clf.fitted_pipeline_, f)
                pickle.dump(scaler, f)

            else:
                last_model = self.clf.fit(self.x_train_trans, self.labels)
                pickle.dump(last_model, f)
                pickle.dump(scaler, f)

        f.close()
        print('Saving thresholds')
               #now saving thresholds
        with open("thresholds.txt", "wb") as fp:
            pickle.dump(self.thresholds, fp)

        with open("xy_from_train.txt", "wb") as fp:
            pickle.dump(self.xy_train_trans, fp)
        
 
    def build_model(self, load=False, grid_search=False, output_conf=None, save=False, cv=None):
         
        ##Preprocess data##
        x_train_trans, mol_not_docked = self.__fit_transform__(self.features)
        self.x_train_trans = np.array(x_train_trans[:,1:]) #DEFUALT 

        #keeping only docked molecules
        if not self.debug: 
            mols_to_maintain = [mol for mol in self.mol_names if mol not in mol_not_docked]
            indxs_to_maintain = [np.where(np.array(self.mol_names) == mol)[0] for mol in mols_to_maintain]
            labels = np.array(self.labels)[indxs_to_maintain]
            self.labels = pd.Series(np.stack(labels, axis=1)[0])
            self.mol_names = mols_to_maintain
            self.n_active_corrected = len([label for label in self.labels if label==1])
            self.n_inactive_corrected = len([label for label in self.labels if label==0])
            if self.cv > self.n_inactive_corrected  or self.cv > self.n_active_corrected:
                 self.cv = min([self.n_active_corrected, self.n_inactive_corrected])
             

        self.headers = self.retrieve_header()[1:]
        if self.columns:
            user_indexes = np.array([self.headers.index(column) for column in self.columns], dtype=int)
            self.x_train_trans = self.x_train_trans[:, user_indexes]        
            self.headers = np.array(self.headers)[user_indexes].tolist()      
        
        if self.imputer == 'column_based':
            imputer = SimpleImputer(strategy='constant')
        if self.imputer == 'sample_based':
            imputer = NewImputer(strategy = 'mean')
       
        self.x_train_trans = imputer.fit_transform(self.x_train_trans)
 
        self.xy_train_trans = [[x,y] for x,y in zip(self.x_train_trans, self.labels)]

        #computing thresholds and adding membership information to the original x
        self.thresholds = dom.threshold(self.x_train_trans, self.labels, self.debug) #computing thresholds

        self.insiders, self.count_active, self.count_inactive, self.distances = dom.evaluating_domain(self.xy_train_trans, self.x_train_trans, self.labels, self.thresholds, self.debug)

        #self.x_train_trans = np.column_stack((self.x_train_trans, np.column_stack((self.insiders,self.count_active,self.count_inactive))))

        #self.headers.append('Total_thresholds')
        #self.headers.append('Active_thresholds')
        #self.headers.append('Inactive_thresholds')
        
        #finally scaling

        scaler = StandardScaler()
        self.x_train_trans = scaler.fit_transform(self.x_train_trans)
       
        ##Classification##
        np.random.seed(7)
        if self.is_stack_model():
            print('Stack model')
            if self.tpot:
                print('Tpot')
                for cl in self.clf[:-1 ]:
                    cl.fit(self.x_train_trans, self.labels)
                preds = np.array([ cl.predict(self.x_train_trans) for cl in self.clf[:-1 ]])
                preds_proba = np.array([ cl.predict_proba(self.x_train_trans) for cl in self.clf[:-1 ]])
                #X = np.hstack( [self.x_train_trans, np.transpose([x[:,0] for x in preds_proba])])
                X = np.hstack( [self.x_train_trans, preds.T])
                #Stack all classifiers in a final one
                last_clf = self.clf[-1]
                last_clf.fit(X, self.labels)
                self.prediction = last_clf.predict(X)
                self.prediction_prob = last_clf.predict_proba(X)
                self.uncertanties = self.calculate_uncertanties(preds) 

                #Obtain results
                clf_result = np.vstack([preds, self.prediction])
                self.clf_results = [] # All classfiers
                for results in clf_result:
                    self.clf_results.append([pred == true for pred, true  in zip(results, self.labels)])
                self.results = [ pred == true for pred, true in zip(self.prediction, self.labels)] #last classifier
            else:
                print("No tpot")
                last_clf = self.clf[-1]
                #Predict with 5 classfiers
                preds = np.array([ cross_val_predict(c, self.x_train_trans, self.labels, cv=self.cv) for c in self.clf[:-1 ]])
                preds_proba = np.array([ cross_val_predict(c, self.x_train_trans, self.labels, cv=self.cv, method='predict_proba') for c in self.clf[:-1 ]])
                #X = np.hstack( [self.x_train_trans, np.transpose([x[:,0] for x in preds_proba])])
                X = np.hstack( [self.x_train_trans, preds.T])
                #Stack all classifiers in a final one
                self.prediction = cross_val_predict(last_clf, X, self.labels, cv=self.cv)
                self.prediction_prob = cross_val_predict(last_clf, X, self.labels, cv=self.cv, method='predict_proba')
                self.uncertanties = self.calculate_uncertanties(preds) 
                #Obtain results
                clf_result = np.vstack([preds, self.prediction])
                self.clf_results = [] # All classfiers
                for results in clf_result:
                    self.clf_results.append([pred == true for pred, true  in zip(results, self.labels)])
                self.results = [ pred == true for pred, true in zip(self.prediction, self.labels)] #last classifier
            
        else:
            print("Normal model")
            if self.tpot:
                self.clf.fit(self.x_train_trans, self.labels)
                self.prediction = self.clf.predict(self.x_train_trans)
                self.prediction_prob = self.clf.predict_proba(self.x_train_trans)
                #Obtain results
                self.results = [ pred == true for pred, true in zip(self.prediction, self.labels)]
            else:
                self.prediction = cross_val_predict(self.clf, self.x_train_trans, self.labels, cv=self.cv)
                self.prediction_prob = cross_val_predict(self.clf, self.x_train_trans, self.labels, cv=self.cv, method='predict_proba')
                #Obtain results
                self.results = [ pred == true for pred, true in zip(self.prediction, self.labels)]

        #Analyse results
        print('Postprocessing')
        if not self.debug:
            self.postprocessing()

        self.saving_model(scaler) #and also saving thresholds

    def postprocessing(self, print_most_important=False, output_conf="conf.png", test=False):
        ##Dimensionallity reduction##
        dim_reduct_folders = ["dimensionallity_reduction", "dimensionallity_reduction/umap",
           "dimensionallity_reduction/tsne", "dimensionallity_reduction/pca"]
        dim_reduct_folders = [direc for direc in dim_reduct_folders]
        for folder in dim_reduct_folders:
            if not os.path.exists(folder):
                os.makedirs(folder)
        with hp.cd(dim_reduct_folders[0]):
            # Plot Sample landscape
            vs.UMAP_plot(self.x_train_trans, self.labels, output="umap/prediction_landscape_umap.png")
            vs.pca_plot(self.x_train_trans, self.labels, output="pca/sample_landscape_pca.png", biplot=self.headers)
            vs.tsne_plot(self.x_train_trans, self.labels, output="tsne/sample_landscape_tsne.png")

            # Plot result each clf
            if self.is_stack_model(): 
                for i, (result, clf_title)  in enumerate(zip(self.clf_results, CLF)):
                    vs.UMAP_plot(self.x_train_trans, result, output="umap/{}{}_umap.png".format(clf_title, i), title=clf_title)
                    vs.pca_plot(self.x_train_trans, result, output="pca/{}{}_pca.png".format(clf_title, i), title=clf_title)
                    vs.tsne_plot(self.x_train_trans, result, output="tsne/{}{}_tsne.png".format(clf_title, i), title=clf_title)
            else:
                vs.UMAP_plot(self.x_train_trans, self.results, output="umap/{}_umap.png".format("result"), title="result")
                vs.pca_plot(self.x_train_trans, self.results, output="pca/{}_pca.png".format("result"), title="result")
                vs.tsne_plot(self.x_train_trans, self.results, output="tsne/{}_tsne.png".format("result"), title="result")

        ##Feature importance##
        feature_folders = ["features"]
        for folder in feature_folders:
            if not os.path.exists(folder):
                os.mkdir(folder)
        with hp.cd(feature_folders[0]):
            #correlation matrice
            correlations = self.correlation_heatmap()

            #Plot features
            #self.plot_features([["Score", "Metal"],]) 

            # Retrieve list with feature importance
            print("\nImportant Features\n")
            important_features = self.feature_importance(clf=None, cv=1, number_feat=100, output_features="glide_features.txt")
            if print_most_important:
                print("\nMost Important Features\n")
                print(" ".join(important_features))
   
        ##Metrics##
        metric_folders = ["metrics"]
        for folder in metric_folders:
            if not os.path.exists(folder):
                os.mkdir(folder)
        with hp.cd(metric_folders[0]):
            # Confusion Matrix
            conf = confusion_matrix(self.labels, self.prediction)
            conf[1][0] += (self.n_initial_active - self.n_final_active)
            conf[0][0] += (self.n_initial_inactive - self.n_final_inactive)
            try: print("{} KFOLD Training Crossvalidation".format(self.cv))
            except : pass
            print(conf.T)
            md.conf(conf[1][1], conf[0][1], conf[0][0], conf[1][0], output=output_conf)

            # ROC/PR CURVE
            self.plot_roc_curve_rate(self.labels, self.prediction_prob, self.prediction)
            self.plot_pr_curve_rate(self.labels, self.prediction_prob, self.prediction)


        ##Model assesment##
        model_folders = ["model"]
        for folder in model_folders:
            if not os.path.exists(folder):
                os.mkdir(folder)
        with hp.cd(model_folders[0]):
            #Report Errors
            print("\nMistaken Samples\n")
            errors = [ self.mol_names[i] for i, v in enumerate(self.results) if not v ]
            if self.is_stack_model(): 
                uncertanties_errors = [ self.uncertanties[i] for i, v in enumerate(self.results) if not v ]
            #np.savetxt("mistaken_samples.txt", errors)
            #np.savetxt("uncertanties.txt", uncertanties_errors)
            print(errors)

    def shap_analysis(self, clf=None, output_folder="."):

        if not os.path.exists(output_folder):
            os.makedirs(output_folder)
        samples = self.mol_names[0:1] if self.debug else self.mol_names
        clf = clf if clf else self.clf

        df = pd.DataFrame(self.x_test_trans, columns=self.headers)
        data_for_prediction_array = df.values
        clf.predict_proba(data_for_prediction_array)

        # Create object that can calculate shap values
        explainer = shap.TreeExplainer(clf)
        # Calculate Shap values
        shap_values = explainer.shap_values(data_for_prediction_array)[0]
            
        for row, name in enumerate(samples):
            shap.force_plot(explainer.expected_value[1], shap_values[row,:], df.iloc[row,:], matplotlib=True, show=False, text_rotation=90, figsize=(40, 10))
            plt.savefig(os.path.join(output_folder, '{}_shap.png'.format(name)))
        fig, axs = plt.subplots()
        shap.summary_plot(shap_values, df, plot_type="bar", show=False, auto_size_plot=True)
        fig.savefig(os.path.join(output_folder, 'feature_importance_shap') )

    def is_stack_model(self):
        return type(self.clf) is list and len(self.clf) > 0

    def plot_features(self, plot_variables):
        """
        plot variables :: list of 2 fields [ ["Score", "Gscore"], ["Logp", "Logd"]]
        It will plot the two variables coloured by label
        """
        for field in plot_variables:
            name1 = field[0]
            name2 = field[1]
            idx1 = self.headers.index(name1)
            idx2 = self.headers.index(name2)
            values1 = self.x_train_trans[:, idx1]
            values2 = self.x_train_trans[:, idx2]
            vs.plot(values1, values2, self.labels, title="{}_{}_true_labels".format(name1, name2), output="{}_{}_true_labels.png".format(name1, name2))
            vs.plot(values1, values2, self.results, true_false=True, title="{}_{}_errors".format(name1, name2), output="{}_{}_errors.png".format(name1, name2))


    def plot_roc_curve(self, y_test, preds, n_classes=2):
        """
        Plot area under the curve
        """
        import scikitplot as skplt
        new_preds = []
        new_trues = []
        for p, t in zip(preds, y_test):
            if p == 0:
                new_preds.append([1, 0])
            else:
                new_preds.append([0, 1])
        skplt.metrics.plot_roc_curve(y_test.values, np.array(new_preds))
        plt.savefig("roc_curve.png")

    def plot_roc_curve_rate(self, y_test, preds, pred, n_classes=2):
        #Plot roc curve
        fpr, tpr, threshold = metrics.roc_curve(y_test, preds[:,1]) #preds contains a tuple of probabilities for each 
        roc_auc = metrics.roc_auc_score(y_test, np.around(preds[:,1]))
        fig, ax = plt.subplots()
        ax.plot(fpr, tpr, 'b', label = 'AUC = %0.2f' % roc_auc)
        ax.legend(loc = 'lower right')
        ax.plot([0, 1], [0, 1],'r--')
        ax.set_xlim([0, 1])
        ax.set_ylim([0, 1])
        ax.set_ylabel('True Positive Rate')
        ax.set_xlabel('False Positive Rate')
        fig.savefig("roc_curve.png")

    def plot_pr_curve_rate(self, y_test, preds, pred, n_classes=2):
        #plot PR curve
        precision, recall, thresholds = precision_recall_curve(y_test, preds[:,1])
        ap = average_precision_score(y_test, np.around(preds[:,1]), average = 'micro')
        fig, ax = plt.subplots()
        ax.plot(recall, precision, alpha=0.2, color='b', label='AP = %0.2f' %ap)
        ax.legend(loc = 'lower right')
        ax.set_xlim([0, 1])
        ax.set_ylim([0, 1])
        ax.set_ylabel('Precision')
        ax.set_xlabel('Recall')
        fig.savefig("PR_curve.png")

    def calculate_uncertanties(self, predictions):
        n_samples = len(predictions[0])
        n_class_predicting_active = [0] * n_samples  
        for pred in predictions:
            for i, sample in enumerate(pred):
                if sample == 1:
                    n_class_predicting_active[i] += 1
        n_classifiers = len(predictions)
        uncertanties = [u/n_classifiers for u in n_class_predicting_active]
        return uncertanties

    def load_model(self):
        print("Loading model")
        data = []
        with open(os.path.join("../from_train/model", self.filename_model), 'rb') as rf:
            try:
                while True:
                    data.append(pickle.load(rf))
            except EOFError:
                pass
        with open(os.path.join("../from_train/thresholds.txt"), "rb") as fp:
            self.threshold_from_train = pickle.load(fp)
        with open(os.path.join("../from_train/xy_from_train.txt"), "rb") as fp:
            self.xy_from_train = pickle.load(fp)
        return data

    def save(self, output):
        print("Saving Model")
        pickle.dump(model, open(output, 'wb'))

    def test_importance(self, names, clf):
        from sklearn.feature_selection import SelectKBest
        from sklearn.feature_selection import chi2        
        from sklearn.preprocessing import QuantileTransformer
        test, label_test, train, label_train = [], [], [], []
        indexes = []
        for i, (m, label) in enumerate(zip(self.features.values, self.labels.values)):
            m = m[0]
            if m.GetProp("_Name") in names:
                test.append(m)
                label_test.append(label)
                indexes.append(i)
            else:
                train.append(m)
                label_train.append(label)

        X_train, mols_not_docked = self.__fit_transform__(pd.DataFrame({"molecules": np.hstack([train, test])}))
        X_train_pos = QuantileTransformer(output_distribution='uniform').fit_transform(X_train)
        X_test = X_train_pos[-2:, :]
        model = clf.fit(X_test, label_test)

        bestfeatures = SelectKBest(score_func=chi2, k=10)
        fit = bestfeatures.fit(X_train_pos, np.hstack([label_train, label_test]))
        dfscores = pd.DataFrame({"Score":fit.scores_})
        dfscores["header"] = self.retrieve_header()
        print(dfscores.nlargest(10,'Score'))
        print(model.predict(X_test))

        bestfeatures = SelectKBest(score_func=chi2, k=10)
        fit = bestfeatures.fit(X_test, label_test)
        dfscores = pd.DataFrame({"Score":fit.scores_})
        dfscores["header"] = self.retrieve_header()
        print(dfscores.nlargest(10,'Score'))
        print(model.predict(X_test))          
        

    def retrieve_header(self, exclude=COLUMNS_TO_EXCLUDE):
        headers = []
        #Return training headers
        if self.pb:
            headers_pb = np.empty(0)
            if self.fp:
                headers_pb = np.hstack([headers_pb, np.loadtxt("daylight_descriptors.txt", dtype=np.str)])
            if self.descriptors:
                headers_pb = np.hstack([headers_pb, np.loadtxt("2D_descriptors.txt", dtype=np.str)])
            if self.MACCS:
                headers_pb = np.hstack([headers_pb, np.loadtxt("MAC_descriptors.txt", dtype=np.str)])
            headers.extend(headers_pb.tolist())
        if self.external_data:
            headers.extend(list(pd.read_csv(os.path.join("descriptors", self.external_data))))
        # Remove specified headers
        headers_to_remove = [feature for field in exclude for feature in headers if field in feature ]
        for header in list(set(headers_to_remove)): 
            headers.remove(header)
        return headers
        
    def feature_importance(self, clf=None, cv=1, number_feat=5, output_features="important_fatures.txt"):
        print("Extracting most importance features")
        assert len(self.headers) == self.x_train_trans.shape[1], "Headers and features should be the same length \
            {} {}".format(len(self.headers), self.x_train_trans.shape[1])
        clf = cl.XGBOOST
        model = clf.fit(self.x_train_trans, self.labels)
        important_features = model.get_booster().get_score(importance_type='gain')
        important_features_sorted = sorted(important_features.items(), key=operator.itemgetter(1), reverse=True)
        important_features_name = [[self.headers[int(feature[0].strip("f"))], feature[1]] for feature in important_features_sorted]
        np.savetxt(output_features, important_features_name, fmt='%s')
        features_name = [ feat[0] for feat in important_features_name ]
        return features_name

    def plot_descriptor(self, descriptor):
        print("Plotting descriptor {}".format(descriptor))
        headers = self.retrieve_header()
        index = headers.index(descriptor)
        data = self.x_train_trans[:, index]
        fig, ax = plt.subplots()
        ax.hist(data)
        fig.savefig("{}_hist.png".format(descriptor))

    def correlation_heatmap(self, output="correlation_map.png"):
        corr = pd.DataFrame(self.x_train_trans).corr()
        fig = plt.figure()
        ax = fig.add_subplot(111)
        cax = ax.matshow(corr,cmap='coolwarm', vmin=-1, vmax=1)
        fig.colorbar(cax)
        ticks = np.arange(0,len(self.headers),1)
        ax.set_xticks(ticks)
        plt.xticks(rotation=90)
        ax.set_yticks(ticks)
        ax.set_xticklabels(self.headers)
        ax.set_yticklabels(self.headers)
        fig.savefig(output)
        return corr

    def correlated_columns(self):
        corr_matrix = pd.DataFrame(self.x_train_trans).corr().abs()
        upper = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(np.bool))
        to_drop = [self.headers[column] for column in upper.columns if any(upper[column] > 0.95)]
        print(to_drop)

    def __predict__(self):
        print("Fitting...")
        #Initial Variables
        cv = self.n_final_active
        scaler = StandardScaler()
        #Preprocess data
        self.x_train_trans, mols_not_docked = self.__fit_transform__(self.features)
        print(self.x_train_trans)
        print("Size train", self.x_train_trans.shape)
        self.x_test_trans, mols_not_docked = self.__fit_transform__(self.data_test) 
        print(self.x_test_trans)
        print("Size test", self.x_test_trans.shape)
        # Fit pre-models
        self.models_fitted = [ c.fit(self.x_train_trans, self.labels) for c in self.clf[:-1 ]]
        #Predict pre-model
        preds_test = np.array([ model.predict(self.x_test_trans) for model in self.models_fitted ])
        # Fit last model
        preds_train = np.array([ cross_val_predict(c, self.x_train_trans, self.labels, cv=self.cv) for c in self.clf[:-1 ]])
        X = np.hstack( [self.x_train_trans, preds_train.T] )
        self.last_model = self.clf[-1].fit(scaler.fit_transform(X), self.labels)
        #Predict last model
        X = np.hstack( [self.x_test_trans, preds_test.T] )
        prediction = self.last_model.predict(scaler.fit_transform(X))
        return prediction
    

    def external_prediction(self):
        print('Predicting over the new dataset....')
        self.headers = self.retrieve_header()[1:]
        loaded_models = self.load_model() #loaded model and also thresholds from training set
        scaler = loaded_models[-1]
        x_test_trans, mol_not_docked = self.__fit_transform__(self.features)
        self.x_test_trans = x_test_trans[:,1:]

        mols_to_maintain = [mol for mol in self.mol_names if mol not in mol_not_docked]
        indxs_to_maintain = [np.where(np.array(self.mol_names) == mol)[0] for mol in mols_to_maintain]
        labels = np.array(self.labels)[indxs_to_maintain]
        #to pandas
        self.labels = pd.Series(np.stack(labels, axis=1)[0])
        self.mol_names = mols_to_maintain
        
        if self.imputer == 'column_based':
            imputer = SimpleImputer(strategy='constant')
        elif self.imputer == 'sample_based':
            imputer = NewImputer(strategy = 'mean')
       
        self.x_test_trans = imputer.fit_transform(self.x_test_trans)

        #evaluating applicability domains and credibilities
        self.insiders, self.count_active, self.count_inactive, self.distances = dom.evaluating_domain(self.xy_from_train, self.x_test_trans, self.labels, self.threshold_from_train, self.debug)
        #adding threshold membership information to the x

        #self.x_test_trans = np.column_stack((self.x_test_trans,np.column_stack((self.insiders,self.count_active,self.count_inactive))))
        #self.headers.append('Total_thresholds')
        #self.headers.append('Active_thresholds')
        #self.headers.append('Inactive_thresholds')
        
        self.x_test_trans = scaler.transform(imputer.fit_transform(self.x_test_trans))
  
 
        if self.is_stack_model():
            self.premodels = loaded_models[:-2]
            #predicting with the individual clasifiers
            predictions = np.array([ model.predict(self.x_test_trans) for model in self.premodels]) 
            predictions_proba = np.array([ model.predict_proba(self.x_test_trans) for model in self.premodels]) 
            #finally aggregate prediction        
            #self.results_ensemble_test = np.hstack( [self.x_test_trans, np.transpose([x[:,0] for x in predictions_proba])])
            self.results_ensemble_test = np.hstack( [self.x_test_trans, predictions.T])
            self.prediction = loaded_models[-2].predict(self.results_ensemble_test)
            self.prediction_prob = loaded_models[-2].predict_proba(self.results_ensemble_test)
            clf_result = np.vstack([predictions, self.prediction])
            self.clf_results = [] # All classfiers
            for results in clf_result:
                self.clf_results.append([pred == true for pred, true  in zip(results, self.labels)])
            self.uncertanties = self.calculate_uncertanties(predictions)
            self.results = [ pred == true for pred, true in zip(self.prediction, self.labels)] #last classifier

        else: 
            self.prediction = np.array(loaded_models[0].predict(self.x_test_trans))
            self.prediction_prob = np.array(loaded_models[0].predict_proba(self.x_test_trans))
            #Obtain results
            self.results = [ pred == true for pred, true in zip(self.prediction, self.labels)]



        #evaluating important features on prediction
        x_train = [x[0] for x in self.xy_from_train] #first component
        y_train = [x[1] for x in self.xy_from_train] #second component
#        clf = RandomForestClassifier(random_state=213).fit(x_train, y_train)
#        self.shap_analysis(clf=clf, output_folder="features/shap")
 
        #plotting distributions for dataset and actives/inactives
        x_test_trans_red = [x[:-3] for x in self.x_test_trans]
#        distributions.evaluating_distributions(x_train, y_train, x_test_trans_red, self.labels)
        dom.analysis_domain(self.mol_names, self.insiders, self.count_active, self.count_inactive, self.distances, self.labels, self.prediction, self.prediction_prob,self.threshold_from_train,self.debug)
        #setting test = train to call postprocessing
        self.x_train_trans = self.x_test_trans  
        if not self.debug:
            self.postprocessing(test=True)

def parse_args(parser):
    parser.add_argument('--active', type=str,
                        help='sdf file with active compounds')
    parser.add_argument('--inactive', type=str,
                        help='sdf file with inactive compounds')
    parser.add_argument('--test', type=str,
                        help='sdf file with test compounds', default=None)
    parser.add_argument('--external_data', type=str,
                        help='csv with external data to add to the model', default="glide_features.csv")
    parser.add_argument('--columns_to_keep', nargs="+", help="Name of columns to be kept from your external data", default=[])
    parser.add_argument('--load', type=str,
                        help='load model from file', default=None)
    parser.add_argument('--save', type=str,
                        help='save model to file', default=None)
    parser.add_argument('--pb', action="store_true",
                        help='Compute physic based model (ligand topology, logP, logD...) or just glide model')
    parser.add_argument('--cv', type=int,
                        help='cross validation k folds', default=None)
    parser.add_argument('--features', type=int,
                        help='Number of important features to retrieve', default=5)
    parser.add_argument('--features_cv', type=int,
                        help='KFold when calculating important features', default=1)
    parser.add_argument('--descriptors', nargs="+", help="descriptors to plot", default=[])
    parser.add_argument('--classifier', type=str, help="classifier to use", default="stack")
    parser.add_argument('--test_importance', nargs="+", help="Name of Molecules to include on testing feature importance", default=[])
    parser.add_argument('--print_most_important', action="store_true", help="Print most important features name to screen to use them as command lina arguments with --columns_to_keep")
    parser.add_argument('--build_model', action="store_true", help='Compute crossvalidation over active and inactives')
    parser.add_argument('--filename_model', default = 'fitted_models.pkl', help='Filename for models')
    parser.add_argument('--tpot', action="store_true", help='Use tpot optimizer')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Build 2D QSAR model')
    parse_args(parser)
    args = parser.parse_args()
    model = GenericModel(args.active, args.inactive, args.classifier, args.filename_model, csv=args.external_data, test=args.test, pb=args.pb, columns=args.columns_to_keep)
    if args.load:
        model = model.load(args.load)
    if args.build_model:
        X_train = model.build_model(model.features, print_most_important=args.print_most_important)
        #model.feature_importance(output_features="lucia.txt")
    if args.save:
        model.save(args.save)
    if args.test:
        prediction = model.__predict__() 
        np.savetxt("results.txt", prediction)
    if args.test_importance:
        model.test_importance(args.test_importance, clf)
