from abc import ABC, abstractmethod
from typing import Dict
from sklearn.ensemble import VotingClassifier
from sklearn.model_selection import RandomizedSearchCV, GridSearchCV
from sklearn.experimental import enable_halving_search_cv  # noqa
from sklearn.model_selection import HalvingRandomSearchCV, HalvingGridSearchCV
from sklearn.metrics import matthews_corrcoef, accuracy_score
from modtox.modtox.ML import _clfs


class HyperparameterTuner(ABC):
    """Base class for hyperparameter tuning
    All subclasses must have a 'search' method that returns
    a FITTED estimator with the best parameters."""

    score: float
    best_params: Dict
    name: str

    def __init__(self, X, y) -> None:
        self.X = X
        self.y = y
        self.estimators = _clfs.estimators  # {'knn': KNeighborsClassifier(),}
        self.distributions = (
            _clfs.distributions
        )  # {'knn': {'p': [1, 2], ...}, 'svc': {...}}
        self.halving_dist = (
            _clfs.halving_dist
        )  # {'knn': {'p': [1, 2], ...}, 'svc': {...}}
        self.estimators_random_search = (
            _clfs.estims
        )  # [('knn', KNeighborsClassifier()), ...]
        self.params_random_search = _clfs.params  # {'knn__p': [1, 2], ...}

    @abstractmethod
    def search(self):
        """Returns a VotingClassifier with the best parameters.
        Must define: 
        self.score: float (best parameters score)
        self.best_params: dict as {param_name: value, ...}"""

    def get_best_params(self, fitted_votclf: VotingClassifier) -> Dict:
        """Gets the params of a VotingClassifier and shows
        the ones present in the distribution. To make it comparable
        to the RandomSearch attibute 'best_params_' """
        params = {
            name: param
            for name, param in fitted_votclf.get_params().items()
            if "__" in name
        }
        best_params = {
            name: param
            for name, param in params.items()
            if name in self.params_random_search
        }
        return best_params


class RandomSearch(HyperparameterTuner):
    def __init__(self, X, y, voting="hard", **kwargs) -> None:
        super().__init__(X, y)
        self.votclf = VotingClassifier(
            estimators=self.estimators_random_search, voting=voting
        )

    def search(
        self, cv=5, n_iter=10, random_state=None, **kwargs
    ) -> RandomizedSearchCV:
        clf = RandomizedSearchCV(
            self.votclf,
            self.params_random_search,
            cv=cv,  # CV splitting
            n_iter=n_iter,  # Number of parameter settings that are sampled. n_iter trades off runtime vs quality of the solution.
            random_state=random_state,  # seed for testing purposes
            verbose=0,
        )
        random_search = clf.fit(self.X, self.y)
        self.score = random_search.best_score_
        self.best_params = random_search.best_params_
        return random_search.best_estimator_


class RandomHalvingSearch(HyperparameterTuner):
    def __init__(self, X, y, voting="hard", score_func=accuracy_score, **kwargs) -> None:
        super().__init__(X, y)
        self.voting = voting
        self.score_func = score_func

    def search(self, random_state=42, **kwargs) -> VotingClassifier:
        best_estimators = list()
        train_scores = dict()
        # Search for each parameter combination of each classifier and create VotingClassifier with best estimators.
        for name, clf in self.estimators.items():
            hclf = HalvingRandomSearchCV(
                clf,
                self.halving_dist[name],
                random_state=random_state,
                scoring=self.score_func,
            )
            fitted_clf = hclf.fit(self.X, self.y)
            y_pred = fitted_clf.best_estimator_.predict(self.X)
            best_estimators.append((name, fitted_clf.best_estimator_))
            train_scores[clf.__class__.__name__] = self.score_func(self.y, y_pred)
        
        votclf = VotingClassifier(
            estimators=best_estimators, voting=self.voting
        )
        fitted_votclf = votclf.fit(self.X, self.y)
        train_scores[votclf.__class__.__name__] = fitted_votclf.score(
            self.X, self.y
        )

        return fitted_votclf, train_scores


# Same class changing the Halving to Grid. Maybe abstract behaviour.
class GridHalvingSearch(HyperparameterTuner):
    def __init__(self, X, y, voting="hard", score_func=accuracy_score, **kwargs) -> None:
        super().__init__(X, y)
        self.voting = voting
        self.score_func = score_func

    def search(self, random_state=42, **kwargs) -> VotingClassifier:
        best_estimators = list()
        train_scores = dict()
        # Search for each parameter combination of each classifier and create VotingClassifier with best estimators.
        for name, clf in self.estimators.items():
            hclf = HalvingGridSearchCV(
                clf,
                self.halving_dist[name],
                random_state=random_state,
                scoring=self.score_func,
            )
            fitted_clf = hclf.fit(self.X, self.y)
            y_pred = fitted_clf.best_estimator_.predict(self.X)
            best_estimators.append((name, fitted_clf.best_estimator_))
            train_scores[clf.__class__.__name__] = self.score_func(self.y, y_pred)
        
        votclf = VotingClassifier(
            estimators=best_estimators, voting=self.voting
        )
        fitted_votclf = votclf.fit(self.X, self.y)
        train_scores[votclf.__class__.__name__] = fitted_votclf.score(
            self.X, self.y
        )

        return fitted_votclf, train_scores

